// work_unit_loop.h — the executable side of docs/WORK_UNIT_CONTRACT.md
// (contract_version 1). The service holds geometry and the OptiX context
// resident and consumes unit specs from <workdir>/inbox one at a time in
// lexicographic unit_id order, writing per-unit results to
// <workdir>/outbox/<unit_id>/ with the done/error marker convention.
//
// Beyond the contract, two launcher conveniences: the loop exits cleanly at
// a unit boundary when <workdir>/stop exists or after --max-units units.
// Relative input paths in file-fed units resolve against <workdir>.
#pragma once

#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>
#include <cuda_runtime_api.h>

#include "sysrap/NP.hh"
#include "sysrap/sphoton.h"
#include "sysrap/sproc.h"
#include "sysrap/SEvt.hh"
#include "sysrap/SEventConfig.hh"
#include "sysrap/OpticksPhoton.h"

#include "CSGOptiX/CSGOptiXService.h"

struct WorkUnitLoop
{
    static constexpr int CONTRACT_VERSION = 1 ;

    // platform-default launch slice (limits.max_photons_per_launch == 0):
    // finite on Windows where the WDDM watchdog resets the device at ~2 s;
    // both defaults keep launches well under a second at measured rates
#if defined(_MSC_VER)
    static constexpr long DEFAULT_SLICE = 2000000 ;
#else
    static constexpr long DEFAULT_SLICE = 4000000 ;
#endif

    CSGOptiXService& cxs ;
    std::string workdir ;
    std::string geometry_edition ;   // resident edition, launcher-supplied
    long        max_units ;          // 0: no limit
    double      U[2] ;               // cap planes for the on-caps count
    long        slice_cap ;          // buffer size set at init; spec slices clamp to it

    long units_since_init = 0 ;
    int  eventID = 0 ;

    std::filesystem::path inbox()  const { return std::filesystem::path(workdir) / "inbox" ; }
    std::filesystem::path outbox() const { return std::filesystem::path(workdir) / "outbox" ; }
    std::filesystem::path stopfile() const { return std::filesystem::path(workdir) / "stop" ; }

    int run();
    bool next_spec(std::filesystem::path& spec, double& stage_wait_s);
    void process(const std::filesystem::path& spec_path, double stage_wait_s);

    static void write_text(const std::filesystem::path& path, const std::string& text);
    static nlohmann::json device_record();
    static void process_record(nlohmann::json& j, long units_since_init);
};


inline void WorkUnitLoop::write_text(const std::filesystem::path& path, const std::string& text)
{
    std::ofstream ofs(path, std::ios::binary);
    ofs << text ;
    ofs.close();
    if(ofs.fail()) std::cerr << "WorkUnitLoop::write_text FAILED for " << path.string() << "\n" ;
}

inline nlohmann::json WorkUnitLoop::device_record()
{
    nlohmann::json d ;
    d["name"] = SEventConfig::DeviceName() ? SEventConfig::DeviceName() : "" ;
    int drv = 0 ;
    cudaError_t rc = cudaDriverGetVersion(&drv);
    d["driver"] = rc == cudaSuccess ? std::to_string(drv) : std::string("cudaDriverGetVersion:") + cudaGetErrorString(rc) ;
#if defined(_MSC_VER)
    d["platform"] = "windows" ;
#else
    d["platform"] = "linux" ;
#endif
    return d ;
}

inline void WorkUnitLoop::process_record(nlohmann::json& j, long units_since_init)
{
    j["units_since_init"] = units_since_init ;
    j["rss_mb"] = int( sproc::ResidentSetSizeKB() / 1024.f ) ;
    size_t vfree = 0, vtotal = 0 ;
    cudaError_t rc = cudaMemGetInfo(&vfree, &vtotal);
    j["vram_mb"] = rc == cudaSuccess ? int((vtotal - vfree)/(1024*1024)) : -1 ;
    if(rc != cudaSuccess) std::cerr << "WorkUnitLoop cudaMemGetInfo failed: " << cudaGetErrorString(rc) << "\n" ;
}

/**
next_spec: lexicographically first inbox spec. Returns false to exit the
loop (stop file present, or max_units reached upstream). Accumulates the
time spent waiting on an empty inbox.
**/

inline bool WorkUnitLoop::next_spec(std::filesystem::path& spec, double& stage_wait_s)
{
    using clock = std::chrono::steady_clock ;
    auto t0 = clock::now();
    for(;;)
    {
        std::error_code ec ;
        if( std::filesystem::exists(stopfile(), ec) ) return false ;

        std::vector<std::string> names ;
        for(const auto& e : std::filesystem::directory_iterator(inbox(), ec))
        {
            std::string n = e.path().filename().string();
            if( n.size() > 10 && n.rfind(".unit.json") == n.size() - 10 ) names.push_back(n);
        }
        if(ec) std::cerr << "WorkUnitLoop inbox scan error: " << ec.message() << "\n" ;
        if(!names.empty())
        {
            std::sort(names.begin(), names.end());
            spec = inbox() / names.front();
            stage_wait_s = std::chrono::duration<double>(clock::now() - t0).count();
            return true ;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
}

/**
process: one unit, spec to result directory. Failures conclude the unit
with error.json in place of done; the spec is removed either way, per the
contract (a spec with neither marker is unprocessed and gets reprocessed).
**/

inline void WorkUnitLoop::process(const std::filesystem::path& spec_path, double stage_wait_s)
{
    using clock = std::chrono::steady_clock ;
    namespace fs = std::filesystem ;
    using json = nlohmann::json ;

    std::string unit_id = spec_path.filename().string();
    unit_id = unit_id.substr(0, unit_id.size() - 10);   // strip .unit.json

    fs::path outdir = outbox() / unit_id ;
    std::error_code ec ;
    fs::create_directories(outdir, ec);
    if(ec) std::cerr << "WorkUnitLoop create_directories error: " << ec.message() << "\n" ;

    std::string stage = "spec" ;
    long n_generated = 0 ;
    std::vector<sphoton> hits ;

    try
    {
        std::ifstream ifs(spec_path, std::ios::binary);
        json spec = json::parse(ifs);

        if( spec.value("contract_version", -1) != CONTRACT_VERSION )
            throw std::runtime_error("contract_version mismatch: " + spec.dump());

        stage = "geometry" ;
        std::string edition = spec.value("geometry_edition", "");
        if( edition != geometry_edition )
            throw std::runtime_error("geometry_edition [" + edition + "] does not match resident [" + geometry_edition + "]");

        stage = "generate" ;
        NP* ip = nullptr ;
        json gen_echo ;
        auto tg0 = clock::now();
        if( spec.contains("input") )
        {
            std::string rel = spec["input"].value("path", "");
            fs::path p = fs::path(rel).is_absolute() ? fs::path(rel) : fs::path(workdir) / rel ;
            ip = NP::Load(p.string().c_str());
            if( ip == nullptr || ip->shape.size() != 3 )
                throw std::runtime_error("failed to load input photons from " + p.string());
            gen_echo = spec["input"] ;
        }
        else
        {
            json g = spec.at("generator");
            if( g.value("type", "") != "gun" || g.value("version", -1) != 1 )
                throw std::runtime_error("unsupported generator block: " + g.dump());
            json prm = g.at("params");
            double I[8] = {
                prm.at("pos").at(0), prm.at("pos").at(1), prm.at("pos").at(2),
                prm.at("dir").at(0), prm.at("dir").at(1), prm.at("dir").at(2),
                prm.at("emin_kev"),  prm.at("emax_kev")
            };
            ip = GeneratePhotons( g.at("count"), g.at("seed"), I, prm.value("fan_mrad", 0.0) );
            gen_echo = g ;
        }
        n_generated = ip->shape[0] ;
        double generate_s = std::chrono::duration<double>(clock::now() - tg0).count();

        stage = "transport" ;
        long slice = DEFAULT_SLICE ;
        if( spec.contains("limits") )
        {
            long m = spec["limits"].value("max_photons_per_launch", 0L);
            if( m > 0 ) slice = m ;
        }
        if( slice > slice_cap ) slice = slice_cap ;   // GPU buffers were sized to slice_cap at init

        SEvt* sev = SEvt::Get_EGPU();
        double transport_s = 0.0 ;
        int launches = 0 ;
        sphoton* pp = reinterpret_cast<sphoton*>(ip->values<float>());
        for(long off = 0 ; off < n_generated ; off += slice)
        {
            long m = std::min(slice, n_generated - off);
            NP* sub = nullptr ;
            if( off == 0 && m == n_generated )
            {
                sub = ip ;    // single launch: no copy
            }
            else
            {
                sub = NP::Make<float>(int(m), 4, 4);
                memcpy( sub->bytes(), pp + off, size_t(m)*sizeof(sphoton) );
            }

            sev->setIndex(eventID);
            SEvt::SetInputPhoton(sub);
            NP* gs = sev->createInputGenstep_configured();
            if( gs == nullptr ) throw std::runtime_error("no input genstep from the input photons");
            gs->set_meta<int>("eventID", eventID);

            auto t0 = clock::now();
            NP* ht = cxs.simulate(gs, eventID);
            transport_s += std::chrono::duration<double>(clock::now() - t0).count();
            launches += 1 ;
            eventID += 1 ;

            long nh = ht ? ht->shape[0] : 0 ;
            if( nh > 0 )
            {
                size_t prev = hits.size();
                hits.resize(prev + size_t(nh));
                memcpy( hits.data() + prev, ht->bytes(), size_t(nh)*sizeof(sphoton) );
            }

            // the loop owns its arrays: without these frees the process grew
            // ~35 MB per 200k-photon unit. All are consumed by this point;
            // the input-photon pointer SEvt holds is replaced at the next
            // SetInputPhoton. A residual ~12 MB/unit remains inside the
            // simphony per-event state (measured, counts unaffected) — the
            // launcher's periodic re-initialization bounds it, and the soak
            // characterizes it per platform.
            delete ht ;
            delete gs ;
            if( sub != ip ) delete sub ;
        }
        delete ip ;
        ip = nullptr ;

        stage = "output" ;
        SaveHits(hits, (outdir / "hits.npy").string());

        unsigned n_refl = 0, n_cap = 0 ;
        for(const sphoton& h : hits)
        {
            if( h.flagmask & BOUNDARY_REFLECT ) n_refl += 1 ;
            if( h.pos.z < U[0] || h.pos.z > U[1] ) n_cap += 1 ;
        }

        units_since_init += 1 ;

        json r ;
        r["contract_version"] = CONTRACT_VERSION ;
        r["unit_id"] = unit_id ;
        r["status"] = "ok" ;
        r["geometry_edition"] = geometry_edition ;
        r["generator"] = gen_echo ;
        r["counts"] = { {"generated", n_generated}, {"wall_absorbed", hits.size()},
                        {"reflected", n_refl}, {"on_caps", n_cap} } ;
        r["timing"] = { {"generate_s", generate_s}, {"transport_s", transport_s},
                        {"us_per_photon", n_generated > 0 ? 1e6*transport_s/double(n_generated) : 0.0},
                        {"launches", launches}, {"stage_wait_s", stage_wait_s} } ;
        r["device"] = device_record() ;
        process_record( r["process"] = json::object(), units_since_init );
        r["failures"] = json::array() ;

        write_text(outdir / "unit.json", r.dump(2));
        write_text(outdir / "done", "");

        std::cout << "synrad-service-unit: " << unit_id
                  << " generated " << n_generated
                  << " wall-absorbed " << hits.size()
                  << " launches " << launches
                  << " transport " << transport_s << " s"
                  << "\n" ;
    }
    catch(const std::exception& e)
    {
        json err ;
        err["contract_version"] = CONTRACT_VERSION ;
        err["unit_id"] = unit_id ;
        err["status"] = "error" ;
        err["stage"] = stage ;
        err["message"] = e.what() ;
        err["counts"] = { {"generated", n_generated}, {"wall_absorbed", hits.size()} } ;
        write_text(outdir / "error.json", err.dump(2));
        std::cerr << "synrad-service-unit: " << unit_id << " FAILED at " << stage << ": " << e.what() << "\n" ;
    }

    std::error_code rec ;
    std::filesystem::remove(spec_path, rec);
    if(rec) std::cerr << "WorkUnitLoop failed to remove spec " << spec_path.string() << ": " << rec.message() << "\n" ;
}

inline int WorkUnitLoop::run()
{
    std::error_code ec ;
    std::filesystem::create_directories(inbox(), ec);
    std::filesystem::create_directories(outbox(), ec);

    std::cout << "synrad-service: work-unit loop on [" << workdir << "]"
              << " geometry_edition [" << geometry_edition << "]"
              << " default_slice " << DEFAULT_SLICE
              << ( max_units > 0 ? " max_units " + std::to_string(max_units) : std::string("") )
              << "\n" ;

    long done = 0 ;
    while( max_units == 0 || done < max_units )
    {
        std::filesystem::path spec ;
        double stage_wait_s = 0.0 ;
        if( !next_spec(spec, stage_wait_s) ) break ;   // stop file
        process(spec, stage_wait_s);
        done += 1 ;
    }
    std::cout << "synrad-service: work-unit loop exit after " << done << " units\n" ;
    return 0 ;
}
