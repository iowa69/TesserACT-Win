// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
//
// ts_selftest.h -- the hidden self-test of the compatibility layer (TS_COMPAT_SELFTEST=1).
// Included only by ts_compat.cpp and ts_selftest.cpp; the engine never sees it.
#pragma once

namespace ts_compat {

// Runs every probe, prints one "key=value" line per probe on stdout (details of a failure on
// stderr) and returns 0 when all passed, 1 otherwise.
int selfTest();

// "linux" or "windows".
const char* platformName();

// Create `path` as a sparse file of `size` bytes without writing its contents. False when the
// file system cannot do that (the probe is then reported as failed rather than filling the disk).
bool makeSparseFile(const char* path, long long size);

}  // namespace ts_compat
