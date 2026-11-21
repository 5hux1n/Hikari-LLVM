cmake_minimum_required(VERSION 3.20)
project(HikariPlugin LANGUAGES C CXX)

find_package(LLVM 22.1.8 EXACT REQUIRED CONFIG)

set(HIKARI_LLVM "${CMAKE_CURRENT_SOURCE_DIR}/../llvm")
set(OBFUSCATION "${HIKARI_LLVM}/lib/Transforms/Obfuscation")

add_library(Hikari MODULE
  HikariPlugin.cpp
  "${OBFUSCATION}/AntiClassDump.cpp"
  "${OBFUSCATION}/AntiDebugging.cpp"
  "${OBFUSCATION}/AntiHooking.cpp"
  "${OBFUSCATION}/BogusControlFlow.cpp"
  "${OBFUSCATION}/ConstantEncryption.cpp"
  "${OBFUSCATION}/CryptoUtils.cpp"
  "${OBFUSCATION}/Flattening.cpp"
  "${OBFUSCATION}/FunctionCallObfuscate.cpp"
  "${OBFUSCATION}/FunctionWrapper.cpp"
  "${OBFUSCATION}/IndirectBranch.cpp"
  "${OBFUSCATION}/Obfuscation.cpp"
  "${OBFUSCATION}/SplitBasicBlocks.cpp"
  "${OBFUSCATION}/StringEncryption.cpp"
  "${OBFUSCATION}/SubstituteImpl.cpp"
  "${OBFUSCATION}/Substitution.cpp"
  "${OBFUSCATION}/Utils.cpp"
)

target_compile_features(Hikari PRIVATE cxx_std_17)
target_link_libraries(Hikari PRIVATE LLVM)
# Import only Hikari's headers so they cannot shadow LLVM 22's headers.
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/include/llvm/Transforms")
file(CREATE_LINK "${HIKARI_LLVM}/include/llvm/Transforms/Obfuscation"
     "${CMAKE_CURRENT_BINARY_DIR}/include/llvm/Transforms/Obfuscation" SYMBOLIC)
target_include_directories(Hikari PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/include")
target_include_directories(Hikari SYSTEM PRIVATE "${LLVM_INCLUDE_DIRS}")
execute_process(COMMAND git rev-parse --short HEAD
  WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
  OUTPUT_VARIABLE HIKARI_REVISION OUTPUT_STRIP_TRAILING_WHITESPACE)
target_compile_definitions(Hikari PRIVATE GIT_COMMIT_HASH="${HIKARI_REVISION}")
