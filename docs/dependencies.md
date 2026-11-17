# Dependencies and licences

Every runtime and test dependency is pinned to an exact version in `requirements.txt` and uses an OSI-approved licence. No dependency is a payment SDK, and none needs a paid account or an API key.

## Direct dependencies (pinned)

| Package | Version | Licence | OSI-approved | Used for |
|---|---|---|---|---|
| streamlit | 1.67.0 | Apache-2.0 | yes | the web UI |
| requests | 2.34.2 | Apache-2.0 | yes | HTTP calls to the local Ollama server and GET-only web fetches |
| pytest | 9.2.0 | MIT | yes | the test suite |
| python-dotenv | 1.2.4 | BSD-3-Clause | yes | reading settings from an untracked `.env` |
| ddgs | 9.16.1 | MIT | yes | free web search with no API key (Discover phase) |
| beautifulsoup4 | 4.15.0 | MIT | yes | turning fetched HTML pages into plain text |

These match the pins in `requirements.txt`, which is the source of truth.

## Notable transitive dependencies

| Package | Pulled in by | Version checked | Licence |
|---|---|---|---|
| primp | ddgs | 2.0.1 | MIT |
| lxml | ddgs | 6.1.3 | BSD-3-Clause |
| click | ddgs | 8.5.1 | BSD-3-Clause |
| soupsieve | beautifulsoup4 | 4.1.1 | MIT |
| typing-extensions | beautifulsoup4 | 4.16.0 | PSF-2.0 |

## Model and services

| Item | Licence or terms | Cost |
|---|---|---|
| Ollama (local model server) | MIT | free, runs locally |
| `qwen3:8b` (default model; `qwen3:4b` / `qwen3:1.7b` for weaker laptops) | Apache-2.0 open weights | free, runs locally |

The scripts under `scripts/` and `evals/` use the Python standard library only.

## How this list was checked

The licence of each installed package was read from its package metadata (`importlib.metadata`), for example:

```bash
python -c "from importlib.metadata import metadata as m; print(m('ddgs')['License-Expression'])"
```

When a dependency is added or upgraded, add it here with its licence and keep the version pinned.
