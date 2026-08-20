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

# SIMPHONY_SRC_DIR is retained for compatibility with existing build recipes
# but no longer used: the installed header set carries SSimulator.h (sysrap
# CMakeLists HEADERS), so headers resolve from the install alone. Mixing the
# source tree in duplicated sysrap headers under two include roots, which
# #pragma once does not deduplicate.
set(SIMPHONY_SRC_DIR "" CACHE PATH "simphony source checkout (unused)")

# Deliberately no simphony::G4CX and no Geant4: the executable is the
# containment proof and its process image must be Geant4-free.
add_executable(synrad_service synrad_service.cc)
target_include_directories(synrad_service PRIVATE
    ${SYNRAD_GUN_DIR}
)
# CUDA::cudart_static first on Windows: the static core embeds cudart_static
# while the exported simphony targets reference shared cudart; satisfying the
# runtime imports from the static library first keeps one runtime in the
# process. A shared-cudart link produced an executable whose SEventConfig
# device logging repeated without terminating.
if(WIN32)
    find_package(CUDAToolkit REQUIRED)
    target_link_libraries(synrad_service CUDA::cudart_static)
endif()
target_link_libraries(synrad_service
    simphony::CSGOptiX
    simphony::QUDARap
    simphony::CSG
    simphony::SysRap
)

install(TARGETS synrad_service)
