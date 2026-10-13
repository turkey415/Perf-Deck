# Third-party software in packaged builds

iPerf Deck itself is MIT licensed (see `LICENSE`). The packaged downloads also contain the
software below. Running from source contains none of it.

## iperf3

Built unmodified from the official ESnet release (the version and checksum are pinned in
`packaging/build_iperf3.sh`). Copyright The Regents of the University of California, through
Lawrence Berkeley National Laboratory. 3-clause BSD licence; the full text, including the notices
for the components iperf3 itself includes (such as cJSON), ships in the package as
`_internal/iperf3/iperf3-LICENSE.txt`. Source: https://github.com/esnet/iperf

iPerf Deck is not affiliated with or endorsed by ESnet, Lawrence Berkeley National Laboratory or
the U.S. Department of Energy.

## Cygwin runtime (Windows package only)

iperf3 has no native Windows port, so the Windows package builds it with Cygwin and ships the
Cygwin runtime DLLs it needs (`cygwin1.dll`) beside it. The Cygwin runtime is licensed under the
LGPL version 3 or later (https://cygwin.com/licensing.html); the licence texts ship in the package
as `_internal/iperf3/COPYING.LESSER.txt` and `COPYING.txt`. The exact runtime version is recorded
in `_internal/iperf3/cygwin-version.txt`, and its source is available from
https://cygwin.com/git/newlib-cygwin.git and any Cygwin mirror. You may replace `cygwin1.dll`
with your own build.

## Python and PyInstaller

The package embeds the Python interpreter and standard library (Python Software Foundation
License, https://docs.python.org/3/license.html) and is started by the PyInstaller bootloader
(GPL with an exception that allows distributing the built application under any licence,
https://pyinstaller.org/en/stable/license.html).
