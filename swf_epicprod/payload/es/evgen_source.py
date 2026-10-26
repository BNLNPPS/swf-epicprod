"""Feed independently opened HepMC3 range files into npsim's normal primary chain.

The installed DDG4 converter is reused. Only the file reader and range-boundary
reset are supplied here; detector, physics and primary handling remain npsim's.
"""
from pathlib import Path


SOURCE = r'''
#include <DDG4/Geant4InputAction.h>
#include <DDG4/Geant4Context.h>
#include <DDG4/EventParameters.h>
#include <DDG4/RunParameters.h>
#include <DDG4/Factories.h>
#include <HepMC3/ReaderAscii.h>
#include <HepMC3/GenEvent.h>
#include <fstream>
#include <memory>
#include <stdexcept>

// DD4hep 1.37 installs the converter library without its HepMC3EventReader.h.
// This is its ABI declaration; conversion is linked from libDDG4HepMC3.
// Interface: AIDASoft/DD4hep DDG4/hepmc/HepMC3EventReader.h (LGPL-3.0).
namespace dd4hep { namespace sim {
class HEPMC3EventReader : public Geant4EventReader {
public:
    explicit HEPMC3EventReader(const std::string&);
    virtual ~HEPMC3EventReader() = default;
    virtual EventReaderStatus readParticles(int, Vertices&, Particles&);
    virtual EventReaderStatus readGenEvent(int, HepMC3::GenEvent&) = 0;
protected:
    std::string m_flow1 = "flow1";
    std::string m_flow2 = "flow2";
};
}}

namespace EpicprodEvgen {
std::string selected_file(const std::string& control) {
    std::ifstream stream(control);
    std::string path;
    if (!std::getline(stream, path) || path.empty() || path.front() != '/')
        throw std::runtime_error("No absolute EVGEN range path in " + control);
    return path;
}

class EpicprodRangeReader : public dd4hep::sim::HEPMC3EventReader {
    std::string m_control, m_current;
    std::unique_ptr<HepMC3::ReaderAscii> m_file;
public:
    explicit EpicprodRangeReader(const std::string& control)
        : HEPMC3EventReader(control), m_control(control) {}
    EventReaderStatus moveToEvent(int) override { return EVENT_READER_OK; }
    EventReaderStatus readGenEvent(int, HepMC3::GenEvent& event) override {
        std::string path = selected_file(m_control);
        if (path != m_current) {
            m_file.reset(new HepMC3::ReaderAscii(path));
            if (m_file->failed()) throw std::runtime_error("Cannot open EVGEN range " + path);
            m_current = path;
        }
        if (!m_file->read_event(event) || m_file->failed()) return EVENT_READER_EOF;
        auto* parameters = context()->event().extension<dd4hep::sim::EventParameters>(false);
        if (!parameters) {
            parameters = new dd4hep::sim::EventParameters();
            context()->event().addExtension<dd4hep::sim::EventParameters>(parameters);
        }
        parameters->setEventNumber(event.event_number());
        parameters->ingestParameters(event);
        if (event.run_info()) {
            auto* run = context()->run().extension<dd4hep::sim::RunParameters>(false);
            if (!run) {
                run = new dd4hep::sim::RunParameters();
                context()->run().addExtension<dd4hep::sim::RunParameters>(run);
            }
            run->ingestParameters(*event.run_info());
        }
        return EVENT_READER_OK;
    }
};

class EpicprodRangeInput : public dd4hep::sim::Geant4InputAction {
    std::string m_control, m_current;
public:
    EpicprodRangeInput(dd4hep::sim::Geant4Context* context, const std::string& name)
        : Geant4InputAction(context, name) {
        declareProperty("RangeControl", m_control);
    }
    void operator()(G4Event* event) override {
        std::string path = selected_file(m_control);
        if (path != m_current) {
            m_current = path;
            m_currentEventNumber = 0;
            m_firstEvent = 0;
        }
        Geant4InputAction::operator()(event);
    }
};
}
DECLARE_GEANT4_EVENT_READER_NS(EpicprodEvgen, EpicprodRangeReader)
DECLARE_GEANT4ACTION_NS(EpicprodEvgen, EpicprodRangeInput)
'''


def configure(simulation, control):
    """Register the file source before npsim initializes the detector."""
    import DDG4
    import ROOT
    if ROOT.gSystem.Load('libDDG4HepMC3') < 0:
        raise RuntimeError('The campaign image lacks DDG4 HepMC3 conversion')
    if not getattr(configure, 'loaded', False):
        if not ROOT.gInterpreter.Declare(SOURCE):
            raise RuntimeError('Cannot build the EVGEN range input adapter')
        configure.loaded = True
    path = str(Path(control).resolve())

    def source(sim):
        action = DDG4.GeneratorAction(DDG4.Kernel(), 'EpicprodRangeInput/RangeInput')
        action.Input = 'EpicprodRangeReader|' + path
        action.RangeControl = path
        action.Sync = 0
        action.AlternativeStableStatuses = sim.physics.alternativeStableStatuses
        action.AlternativeDecayStatuses = sim.physics.alternativeDecayStatuses
        return action

    simulation.inputConfig.userInputPlugin = [source]
