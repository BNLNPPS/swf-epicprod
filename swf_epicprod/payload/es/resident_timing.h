#pragma once
#include <DDG4/Geant4Kernel.h>
#include <DDG4/Geant4EventAction.h>
#include <chrono>
#include <fstream>
#include <iomanip>
#include <stdexcept>

namespace epicprod {
class ResidentTiming : public dd4hep::sim::Geant4EventAction {
public:
  double first = 0, last = 0;
  std::string marker;
  explicit ResidentTiming(dd4hep::sim::Geant4Context* context)
      : Geant4EventAction(context, "ResidentEventTiming") {}
  static double now() {
    return std::chrono::duration<double>(
        std::chrono::system_clock::now().time_since_epoch()).count();
  }
  void reset(const std::string& path) { first = last = 0; marker = path; }
  void begin(const G4Event*) override {
    if (first == 0) {
      first = now();
      std::ofstream out(marker);
      out << std::setprecision(17) << first << '\n';
      if (!out) throw std::runtime_error("Cannot record first simulation event");
    }
  }
  void end(const G4Event*) override { last = now(); }
};
inline ResidentTiming* installTiming(dd4hep::sim::Geant4Kernel* kernel) {
  auto* action = new ResidentTiming(kernel->workerContext());
  kernel->eventAction().adopt(action);
  return action;
}
}
