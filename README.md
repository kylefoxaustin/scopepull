# scopepull

One-shot stack puller for Unistellar Odyssey Pro telescopes — connect to the
scope's Wi-Fi, run `scopepull`, and every observation you don't already have
lands on disk as verified, organized FITS. Linux + Windows.

> ## 🙏 Standing on prior art
> The hard part of this project — reverse-engineering the scope's HTTP API and
> discovering that **the backend won't stream data unless a client is
> concurrently long-polling `/api/event`** — is the work of
> [**vamshikesireddy/unistellar-data-downloader**](https://github.com/vamshikesireddy/unistellar-data-downloader)
> (MIT). scopepull reuses that endpoint knowledge (see [docs/API.md](docs/API.md))
> in a rewritten async, manifest-aware architecture. If you just want a simple
> "pick observations, get zips" downloader, **use their tool** — it's excellent
> at exactly that. scopepull exists for the archival-sync use case: idempotent
> re-runs, verified atomic ingest, an organized archive layout, and unattended
> operation.

## Status

**Pre-alpha, under active development.** Phases 0-2 complete: protocol recon
(validated against a real Odyssey Pro), library core, and the full
pull -> verify -> ingest pipeline (`scopepull pull`). Deep-sky observations
arrive as raw GBRG Bayer TIFF frames + master dark + the scope's own stack;
ingest keeps the TIFFs and writes per-frame FITS. See [docs/API.md](docs/API.md)
for the measured protocol details.

## Quickstart (will be)

```bash
uv tool install scopepull    # or: pipx install scopepull
# join the scope's Wi-Fi (Odyssey-xxxx on Odyssey/Pro; UNI-xxxx / eVscope-xxxx on older models)
scopepull doctor             # connectivity + DDD check
scopepull                    # pull everything new
```

⚠️ **Warnings that matter**

- Enable **Direct Data Download** first: Unistellar app → Settings → your
  telescope → Download → Direct Data Download.
- **Don't operate the scope while pulling**, and don't let your laptop sleep
  mid-transfer. Interrupted downloads have been observed to corrupt/lose
  observations *on the scope itself*.
- The USB-A port is power-out only. There is no USB data path; Wi-Fi is it.

## License

MIT — see [LICENSE](LICENSE).
