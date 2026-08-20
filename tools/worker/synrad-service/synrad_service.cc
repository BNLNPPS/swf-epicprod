// synrad_service.cc — standalone GPU synchrotron-radiation transport with no
// Geant4 in the process (VOLUNTEER_GPU_PLAN.md, synchrotron-radiation track).
//
// The containment counterpart of the simphony examples/synrad GPU app: the
// same soft X-ray reflect-or-absorb transport (qsim::propagate_gamma with
// QGXS surface parameters, photon energy in the sphoton wavelength slot in
// keV), but the geometry comes from a persisted CSGFoundry bundle resolved
// by the GEOM and <GEOM>_CFBaseFromGEOM envvars instead of a GDML loaded
// through Geant4, and the input photons come from the shared gun
// (synrad_gun.h: bit-identical mt19937 stream for a given seed) or from a
// .npy file. The coprocessor contract: photons in, wall-absorption hits
// out, both as (N,4,4) float32 sphoton arrays.
//
// Usage mirrors examples/synrad/synrad minus the -g GDML argument:
//   synrad_service [-n N] [-s SEED] [-r SIGMA_NM] [-T T_UM] [-U ZLO,ZHI]
//                  [-I x,y,z,dx,dy,dz,emin_keV,emax_keV] [-f FAN_MRAD]
//                  [-i input_photons.npy] [-o OUTDIR]
//
// Work-unit mode (docs/WORK_UNIT_CONTRACT.md): -w WORKDIR -g GEOMETRY_EDITION
// [-N MAX_UNITS] replaces the single-shot input arguments; the service holds
// geometry and the OptiX context resident and consumes WORKDIR/inbox specs.

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "synrad_gun.h"

#include "sysrap/OPTICKS_LOG.hh"
#include "sysrap/SEventConfig.hh"
#include "sysrap/SEvt.hh"

#include "CSGOptiX/CSGOptiXService.h"
#include "qudarap/QGXS.hh"
#include "qudarap/QSim.hh"

#include "work_unit_loop.h"

static void usage(const char* prog)
{
    std::cerr << "Usage: " << prog
              << " [-n N] [-s SEED] [-r SIGMA_NM] [-T T_UM]"
                 " [-U ZLO,ZHI] [-I x,y,z,dx,dy,dz,emin_keV,emax_keV] [-f FAN_MRAD]"
                 " [-i input_photons.npy] [-o OUTDIR]\n" ;
}

int main(int argc, char** argv)
{
    std::string input ;
    std::string outdir = "." ;
    std::string workdir ;
    std::string geometry_edition ;
    long     max_units = 0 ;
    int      n = 500000 ;
    unsigned seed = 42 ;
    double   sigma_nm = 50. ;
    double   T_um = 10. ;
    double   U[2] = { 25.0, 49975.0 } ;                    // benchmark tunnel end plates, mm
    double   I[8] = { 0., 0., 100., 0., 0., 1., 0.3, 19.4 } ;
    double   fan_mrad = 0.5 ;

    for(int i=1 ; i < argc ; i++)
    {
        std::string arg = argv[i] ;
        if(      arg == "-i" && i+1 < argc ) input = argv[++i] ;
        else if( arg == "-o" && i+1 < argc ) outdir = argv[++i] ;
        else if( arg == "-w" && i+1 < argc ) workdir = argv[++i] ;
        else if( arg == "-g" && i+1 < argc ) geometry_edition = argv[++i] ;
        else if( arg == "-N" && i+1 < argc ) max_units = atol(argv[++i]) ;
        else if( arg == "-n" && i+1 < argc ) n = atoi(argv[++i]) ;
        else if( arg == "-s" && i+1 < argc ) seed = unsigned(atoi(argv[++i])) ;
        else if( arg == "-r" && i+1 < argc ) sigma_nm = atof(argv[++i]) ;
        else if( arg == "-T" && i+1 < argc ) T_um = atof(argv[++i]) ;
        else if( arg == "-f" && i+1 < argc ) fan_mrad = atof(argv[++i]) ;
        else if( arg == "-U" && i+1 < argc ) sscanf(argv[++i], "%lf,%lf", U, U+1) ;
        else if( arg == "-I" && i+1 < argc ) sscanf(argv[++i], "%lf,%lf,%lf,%lf,%lf,%lf,%lf,%lf", I, I+1, I+2, I+3, I+4, I+5, I+6, I+7) ;
        else if( arg == "-h" || arg == "--help" ){ usage(argv[0]) ; return 0 ; }
        else { std::cerr << "Unknown argument: " << arg << "\n" ; usage(argv[0]) ; return 1 ; }
    }

    bool unit_mode = !workdir.empty() ;
    if( unit_mode && geometry_edition.empty() )
    {
        std::cerr << "FATAL: work-unit mode (-w) requires the resident geometry edition (-g)\n" ;
        return 1 ;
    }

    NP* ip = nullptr ;
    bool generated = input.empty() ;
    if( unit_mode )
    {
        // photons come per unit; buffers are sized to the launch slice below
    }
    else if( !generated )
    {
        ip = NP::Load(input.c_str());
        if( ip == nullptr || ip->shape.size() != 3 ){ std::cerr << "FATAL: failed to load " << input << "\n" ; return 3 ; }
        n = ip->shape[0] ;
    }
    else
    {
        ip = GeneratePhotons(n, seed, I, fan_mrad);   // same seed -> identical photons to the synrad/synrad_g4 modes
    }

    // gun-generated photons are persisted as a reference-set artifact: the
    // std::mt19937 stream is portable but the distribution implementations
    // are not (libstdc++ vs MSVC), so cross-platform comparison must be fed
    // the photon array, not the gun parameters
    if( !unit_mode && generated )
    {
        std::string ipath = outdir + "/synrad_service_inphoton.npy" ;
        ip->save(ipath.c_str());
        std::cout << "synrad-service-inphoton: " << n << " photons | " << ipath << std::endl ;
    }

    OPTICKS_LOG(argc, argv);

    // same event configuration as examples/synrad/synrad.cpp: hits are the
    // gamma-mode wall kills (BULK_ABSORB), grazing reflection chains reach
    // tens of bounces, the slot heuristic would otherwise size buffers to
    // ~87% of VRAM, and the 1 um propagate epsilon keeps dihedral-edge
    // neighbour facets visible
    // work-unit mode sizes the GPU buffers once to the launch-slice cap;
    // per-unit launches are clamped to it
    long slice_cap = unit_mode ? WorkUnitLoop::DEFAULT_SLICE : long(n) ;

    SEventConfig::SetEventMode("Hit");
    SEventConfig::SetHitMask("AB");
    SEventConfig::SetMaxPhoton(int(slice_cap));
    if( getenv("OPTICKS_MAX_BOUNCE") == nullptr ) SEventConfig::SetMaxBounce(100);
    if( getenv("OPTICKS_MAX_SLOT")   == nullptr ) SEventConfig::SetMaxSlot(int(slice_cap));
    if( getenv("OPTICKS_PROPAGATE_EPSILON") == nullptr ) SEventConfig::SetPropagateEpsilon(0.001f);

    // SEvt(EGPU) + CSGFoundry::Load() + CSGOptiX::Create — no Geant4 anywhere
    CSGOptiXService cxs ;
    if( cxs.fd == nullptr ){ std::cerr << "FATAL: CSGFoundry::Load failed -- check GEOM and <GEOM>_CFBaseFromGEOM envvars\n" ; return 2 ; }
    if( cxs.cx == nullptr ){ std::cerr << "FATAL: CSGOptiX::Create failed (no CUDA device?)\n" ; return 2 ; }

    QGXS qgxs_{ float(sigma_nm*1e-6), float(T_um*1e-3), float(U[0]), float(U[1]) };
    QSim* qs = QSim::Get();
    if( qs == nullptr ){ std::cerr << "FATAL: no QSim -- GPU setup was skipped (no CUDA device?)\n" ; return 2 ; }
    qs->setGXS(qgxs_.d_gxs);

    if( unit_mode )
    {
        WorkUnitLoop loop{ cxs, workdir, geometry_edition, max_units, { U[0], U[1] }, slice_cap };
        return loop.run();
    }

    SEvt* sev = SEvt::Get_EGPU();
    sev->setIndex(0);
    SEvt::SetInputPhoton(ip);
    NP* gs = sev->createInputGenstep_configured();
    if( gs == nullptr ){ std::cerr << "FATAL: no input genstep from the input photons\n" ; return 2 ; }
    gs->set_meta<int>("eventID", 0);

    auto t0 = std::chrono::steady_clock::now();
    NP* ht = cxs.simulate(gs, 0);
    auto t1 = std::chrono::steady_clock::now();
    double t_s = std::chrono::duration<double>(t1 - t0).count();

    std::vector<sphoton> hits(size_t(ht ? ht->shape[0] : 0));
    if( !hits.empty() ) memcpy( hits.data(), ht->bytes(), hits.size()*sizeof(sphoton) );

    std::string path = outdir + "/synrad_service_hits.npy" ;
    SaveHits(hits, path);
    Summary("synrad-service", n, hits, U, t_s, path);
    return 0 ;
}
