TesserACT-Win
=============
Trim and assemble bacterial genomes on Windows.
IOWA-BioTech - Giovanni Lorenzin

How to start
------------
1. Extract the whole zip first (right-click the zip, "Extract All..."). Do not start
   the program from inside the zip.
2. Open the "TesserACT-Win" folder and double-click "TesserACT-Win.exe".
3. If Windows shows "Windows protected your PC", click "More info", then "Run anyway".
   SMARTSCREEN.txt explains why this appears.

Keep the folder together: TesserACT-Win.exe needs the "_internal" and "engines"
folders beside it. You can put the whole folder anywhere, including a USB stick.

How to use it
-------------
1. Samples    Add your read files (two per sample: Read 1 and Read 2) or a whole
              folder, and choose where the results should go.
2. Trimming   Optional. The standard settings are already ticked.
3. Assembly   Optional. The standard settings suit most bacterial isolates.
4. Start      Samples are trimmed and assembled one at a time.

New here? Choose "Try the example" on the Samples page. It runs a small made-up
isolate through the whole program in about a minute.

Where the results are
---------------------
In the results folder you chose:

  run_summary.html      start here: every sample, what happened, and links
  assemblies\           one FASTA file per sample, named after the sample
  <sample>\assembly\    the full TesserACT output for that sample
  <sample>\trimming\    the trimmed reads and the trimming report

What it needs
-------------
Windows 10 or 11, 64-bit. No installation, no administrator rights, no internet.
About 4 GB of free memory per assembly; more for large or very deep samples.

What it does not do
-------------------
It assembles paired-end Illumina reads from bacterial isolates. It does not use
TesserACT's organism models, and it does not handle single-end or long reads.

Help, source code and new versions
----------------------------------
https://github.com/iowa69/TesserACT-Win
TesserACT, the assembler: https://github.com/iowa69/TesserACT

Research software. Not a diagnostic device.
MIT licence: see LICENSE.txt. Third-party notices: NOTICE.txt.
