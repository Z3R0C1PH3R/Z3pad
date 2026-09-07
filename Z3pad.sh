#!/bin/bash
progdir=$(cd $(dirname $0); pwd)
exec >"$progdir/Z3pad-logfile.txt" 2>&1
cd $progdir/Z3pad
python3 z3pad.py
