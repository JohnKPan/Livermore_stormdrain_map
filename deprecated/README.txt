Deprecated -- superseded, kept for reference
===========================================

Nothing in here is called by the pipeline, and nothing imports from here.
Each file was replaced by something that does the same job for every city
rather than for one, and each is kept only because it is the baseline its
replacement was checked against. Deleting them would delete that check.

Every path inside them is relative to the PROJECT ROOT, one level up, so run
them from the root and not from this directory:

    python deprecated/fetch_livermore_inlets.py

run_pipeline.sh
    Replaced by: python run_pipeline.py --city <slug>
    Same steps, same order, same defaults; verified on Hayward -- identical
    file names across all three corpora, zero differences over 8,904 index
    rows and 13 computed columns. What run_pipeline.py fixes is the launcher,
    not the pipeline: this is bash, and on Windows `bash` resolves to WSL from
    PowerShell, which has no cygpath and so exports GDAL_DATA as /mnt/d/...
    that the Windows interpreter cannot read. Editing it mid-run also corrupts
    the run, because bash reads a script incrementally.

fetch_livermore_inlets.py
    Replaced by: python fetch_inlets.py livermore
    Same layer, same canonical field names, plus coded-domain decoding, an OID
    high-water-mark fallback for servers that ignore resultOffset, retry with
    backoff, and a `source` column so several cities share one file.

fetch_livermore_street_centerlines.py
    Replaced by: python fetch_overture_streets.py --cities <city> --roads-only
    One source that works for all 101 cities beat two that disagree about which
    city they serve. Livermore's portal layer was the baseline the Overture
    corpus was checked against -- same OBJECTID set, zero differing property
    values, coordinates identical to the last decimal. It is also the only
    schema with no road_flags, so a corpus built from it cannot mark bridges
    or tunnels on a profile.

See readme.txt in the project root for the pipeline these were part of.
