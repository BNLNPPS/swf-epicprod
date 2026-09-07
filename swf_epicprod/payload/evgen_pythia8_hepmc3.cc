// evgen_pythia8_hepmc3 — the Pythia 8 driver of the payload's internal
// EVGEN stage (docs/EPICPROD_INTERNAL_EVGEN.md): read a command file,
// generate the events it names, write them as HepMC3 ASCII.
//
// Compiled in the job by evgen_generate.py against the campaign image's
// Pythia and HepMC3, since the image builds Pythia without its Python
// binding. Kept to what the stage needs: the steering is the command
// file, the writer is Pythia's own HepMC3 interface, and the afterburner
// runs afterwards on the file.
//
//   evgen_pythia8_hepmc3 <card.cmnd> <out.hepmc>
//
// Exit 0 with the events written; 1 when Pythia fails to initialize or
// aborts more events than Main:timesAllowErrors allows; 2 on usage.
#include <iostream>
#include <string>

#include "Pythia8/Pythia.h"
#include "Pythia8Plugins/HepMC3.h"

int main(int argc, char* argv[]) {
  if (argc < 3) {
    std::cerr << "usage: evgen_pythia8_hepmc3 <card.cmnd> <out.hepmc>\n";
    return 2;
  }
  const std::string card = argv[1];
  const std::string out = argv[2];

  Pythia8::Pythia pythia;
  if (!pythia.readFile(card)) {
    std::cerr << "ERROR: could not read the command file " << card << "\n";
    return 1;
  }
  const int nEvent = pythia.mode("Main:numberOfEvents");
  const int nAbort = pythia.mode("Main:timesAllowErrors");
  if (!pythia.init()) {
    std::cerr << "ERROR: Pythia initialization failed\n";
    return 1;
  }

  Pythia8::Pythia8ToHepMC toHepMC(out);
  int written = 0, aborted = 0;
  for (int i = 0; i < nEvent; ++i) {
    if (!pythia.next()) {
      if (++aborted < nAbort) continue;
      std::cerr << "ERROR: event generation aborted " << aborted
                << " times; giving up after " << written << " events\n";
      pythia.stat();
      return 1;
    }
    toHepMC.writeNextEvent(pythia);
    ++written;
  }
  pythia.stat();
  std::cout << "evgen_pythia8_hepmc3: " << written << " events written to "
            << out << " (" << aborted << " aborted)\n";
  return 0;
}
