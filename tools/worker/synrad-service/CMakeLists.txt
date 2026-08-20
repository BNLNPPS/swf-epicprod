cmake_minimum_required(VERSION 3.22)

project(synrad_service LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# glm before simphony: the installed simphony::SysRap target's link interface
# references glm::glm but the package's Config.cmake does not declare it as a
# dependency (same ordering as simphony examples/synrad).
find_package(glm REQUIRED)
find_package(simphony REQUIRED)

# Directory holding synrad_gun.h (simphony examples/synrad), shared so the
# gun stream is bit-identical to the synrad/synrad_g4 validation runs.
set(SYNRAD_GUN_DIR "" CACHE PATH "directory containing synrad_gun.h")
if(NOT SYNRAD_GUN_DIR)
    message(FATAL_ERROR "SYNRAD_GUN_DIR must point at the simphony examples/synrad directory")
endif()

# The simphony source checkout the install was built from. The installed
# sysrap header set omits SSimulator.h, which CSGOptiX.h includes publicly,
# so sysrap headers are resolved from the source tree — identical files,
# same checkout.
set(SIMPHONY_SRC_DIR "" CACHE PATH "simphony source checkout")
if(NOT SIMPHONY_SRC_DIR)
    message(FATAL_ERROR "SIMPHONY_SRC_DIR must point at the simphony source checkout")
endif()

# Deliberately no simphony::G4CX and no Geant4: the executable is the
# containment proof and its process image must be Geant4-free.
add_executable(synrad_service synrad_service.cc)
target_include_directories(synrad_service PRIVATE
    ${SYNRAD_GUN_DIR}
    ${SIMPHONY_SRC_DIR}/sysrap
)
target_link_libraries(synrad_service
    simphony::CSGOptiX
    simphony::QUDARap
    simphony::CSG
    simphony::SysRap
)

install(TARGETS synrad_service)
