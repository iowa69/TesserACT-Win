// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
//
// sys/resource.h for MinGW-w64, which has none. src/util.cpp still contains its POSIX memory
// queries (renamed out of the way by ts_compat.h and never called); they must compile.
// getrusage() is implemented in ts_compat.cpp.
// Deliberately not <winsock2.h> or <windows.h>: they define the macros `near`, `far`, `min`, `max`,
// and the engine has variables called `near`.
#pragma once
#include <sys/time.h>   // struct timeval
#define RUSAGE_SELF 0
struct rusage {
    struct timeval ru_utime;
    struct timeval ru_stime;
    long ru_maxrss;     // kilobytes
};
int getrusage(int who, struct rusage* out);
