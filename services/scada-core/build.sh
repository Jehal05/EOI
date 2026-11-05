#!/usr/bin/env bash
# JDK-only build (no Maven/Gradle needed). Usage: ./build.sh [test]
set -e
cd "$(dirname "$0")"
export JAVA_TOOL_OPTIONS=""
rm -rf out && mkdir -p out
javac -Xlint:all -d out $(find src -name '*.java')
if [ "$1" = test ]; then
  mkdir -p out-test
  javac -Xlint:all -cp out -d out-test $(find test -name '*.java')
  java -cp out:out-test gs.core.Tests
fi
