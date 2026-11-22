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

include(CTest)
if(BUILD_TESTING)
  add_executable(HikariCryptoUtilsTest
    tests/CryptoUtilsTest.cpp
    "${OBFUSCATION}/CryptoUtils.cpp")
  target_compile_features(HikariCryptoUtilsTest PRIVATE cxx_std_17)
  target_link_libraries(HikariCryptoUtilsTest PRIVATE LLVM)
  target_include_directories(HikariCryptoUtilsTest PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/include")
  target_include_directories(HikariCryptoUtilsTest SYSTEM PRIVATE "${LLVM_INCLUDE_DIRS}")
  add_test(NAME hikari-crypto COMMAND HikariCryptoUtilsTest)

  if(APPLE)
    find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
    set(HIKARI_TEST_APPLE_CLANG "" CACHE FILEPATH
      "Apple Clang for backend tests (defaults to xcrun --find clang)")
    set(hikari_apple_args)
    if(HIKARI_TEST_APPLE_CLANG)
      list(APPEND hikari_apple_args --apple "${HIKARI_TEST_APPLE_CLANG}")
    endif()

    foreach(test IN ITEMS smoke regressions apple_backend)
      set(test_args)
      if(test STREQUAL "apple_backend")
        set(test_args ${hikari_apple_args})
      endif()
      add_test(NAME hikari-${test}
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/${test}.py"
          --llvm-bin "${LLVM_TOOLS_BINARY_DIR}"
          --plugin "$<TARGET_FILE:Hikari>"
          --output "${CMAKE_CURRENT_BINARY_DIR}/test-results/${test}"
          ${test_args})
      set_tests_properties(hikari-${test} PROPERTIES
        ENVIRONMENT "PYTHONDONTWRITEBYTECODE=1" TIMEOUT 600)
    endforeach()
  endif()
endif()
