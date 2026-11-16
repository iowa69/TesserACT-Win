// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
//
// sys/wait.h for MinGW-w64, which has none. Only the status macros that src/mappolish.cpp applies
// to the value of pclose(), which the compatibility layer makes fail (-1) on Windows.
#pragma once
#define WIFEXITED(s)   (1)
#define WEXITSTATUS(s) (s)
#define WIFSIGNALED(s) (0)
#define WTERMSIG(s)    (0)
