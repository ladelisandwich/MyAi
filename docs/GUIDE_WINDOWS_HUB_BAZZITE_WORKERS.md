# Guide: Windows 11 hub + Bazzite GPU workers

A step-by-step setup for this layout:

| Computer | Role in HomeMesh | GPU(s) | What it does |
|---|---|---|---|
| **Windows 11 PC** | coordinator (+ a light worker) | AMD Radeon RX 5700 (8 GB) and NVIDIA GTX 745 (4 GB) | Stores chats, memories and the document index; serves the web app; runs the embedding model |
| **Bazzite desktop** | worker | NVIDIA RTX 3080 | Main chat model |
| **Bazzite laptop** | worker | NVIDIA RTX 4070 Laptop GPU (8 GB) | Second chat machine when it's on and plugged in |

The Bazzite machines connect **out** to the Windows PC; they need no open ports. Only the Windows
PC accepts connections (port 8765, HTTPS, from your home network).

> **What has been tested:** the Windows installer is exercised on GitHub's Windows runners under
> Windows PowerShell 5.1: install, setup, start, web UI, chat, upgrade and uninstall. The Linux
> installer, Ollama installer and a real chat run as a normal user inside the official
> `bazzite-nvidia-open` image, on CPU. Nothing has run on *your* GPUs yet, so step 5 tells you
> what to check.

Total time: about 45 minutes, most of it downloads (Ollama is about 1.4 GB per machine, plus models).

---

## 0. Before you start

1. **Give the Windows PC a fixed address on your network.** Workers remember the address they joined,
   so it must not change. In your router's settings, add a *DHCP reservation* for the Windows PC. To
   find its current address, run `ipconfig` in PowerShell and look at "IPv4 Address", for example
   `192.168.1.20`. This guide calls it **`WIN-IP`**.
2. **Sign in to GitHub on each computer.** The repository is private.
3. All three computers must be on the same home network (or the same VPN, such as Tailscale).

---

## 1. Windows 11 PC (the hub)

Run everything in a **normal** (not administrator) PowerShell window.

### 1.1 Drivers and tools

* **AMD Adrenalin** driver for the RX 5700 (it includes Vulkan, which Ollama uses for this card).
* **NVIDIA driver 570 or newer** for the GTX 745. Ollama needs 570+ for Maxwell cards, and the R580
  branch is the last that supports them.
* Install Git, Python, Node.js and Ollama. Use their websites (git-scm.com, python.org, nodejs.org
  LTS, ollama.com/download) or `winget`:

  ```powershell
  winget install Git.Git
  winget install Python.Python.3.13
  winget install OpenJS.NodeJS.LTS
  winget install Ollama.Ollama
  ```

  Python 3.13 and 3.14 are both tested. **Close and reopen PowerShell** afterwards so the new commands
  are found.

### 1.2 Get HomeMesh and install it

```powershell
git clone --branch claude/stoic-bohr-fkoz0p https://github.com/ladelisandwich/MyAi.git $HOME\homemesh
cd $HOME\homemesh
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -AutoStart
```

Git opens a browser window to sign in to GitHub the first time. The installer:

* creates `%LOCALAPPDATA%\Programs\HomeMesh`
* builds the web app with Node.js
* adds `homemesh` to your user PATH
* with `-AutoStart`, starts HomeMesh when you log in

No administrator rights are needed. **Open a new PowerShell window** afterwards.

### 1.3 Set up the hub

```powershell
homemesh setup
```

Answer:

* **How will this computer be used?** `1` (Standalone). This PC is the hub *and* keeps a small worker
  of its own for embeddings.
* **Name for this computer:** for example `windows-hub`.
* **Allow other devices on your home network to connect?** `y`. This turns on HTTPS with HomeMesh's
  own certificate and prints a **CA fingerprint**; you don't need to copy it now.
* **Owner username / password:** the account you'll sign in with (password at least 10 characters).
* **Start HomeMesh automatically when you log in?** `y`.

Then tell HomeMesh to keep this PC's own worker in the background:

```powershell
notepad (homemesh config path)
```

In the `[worker]` section, change the `mode` line to the following, then save:

```toml
mode = "background"
```

`background` means chats go to the Bazzite GPUs first. This PC still does document indexing and
embeddings, and takes chats only if no other computer can.

### 1.4 Network profile and firewall

1. Settings → Network & internet → your Wi-Fi or Ethernet connection → **Network profile type: Private**.
2. `homemesh start`. The first time it listens on the network, Windows asks whether to allow
   **Python**. Tick **Private networks** only and click **Allow**.
   If you missed the prompt: Windows Security → Firewall & network protection → *Allow an app through
   firewall* → allow `pythonw.exe` / `python.exe` for **Private**.
3. `homemesh status` should say `ok`.

### 1.5 Models on the hub

```powershell
ollama pull nomic-embed-text
```

This is the embedding model for document search (about 270 MB). Optionally add a small chat model for
when the Bazzite PCs are off: `ollama pull llama3.2:3b`.

**Optional: choose which GPU Ollama uses on this PC.** Ollama picks for itself between the RX 5700
(through Vulkan) and the GTX 745 (through CUDA). For embeddings either is fine, so you can skip this.
To pin it to the RX 5700:

1. After a first `ollama run llama3.2:3b "hi"`, open `%LOCALAPPDATA%\Ollama\server.log`. Find the
   `inference compute` lines, which list each GPU with its `library` (CUDA or Vulkan), `id` and name.
2. Set two user environment variables (Settings → search "environment variables" → *Edit
   environment variables for your account*), then quit Ollama from the tray and start it again:
   * `CUDA_VISIBLE_DEVICES` = `-1` stops Ollama using CUDA, and so the GTX 745 through CUDA. It also
     hides NVIDIA GPUs from other CUDA programs you start, though games don't use CUDA.
   * `GGML_VK_VISIBLE_DEVICES` = the Vulkan id the log showed for the RX 5700. Without it, Ollama could
     use the GTX 745 through Vulkan instead.
3. Tell HomeMesh to leave the GTX 745 out of its scheduling: add `ignore_gpus = ["GTX 745"]` under
   `[worker]` in `homemesh.toml`, then `homemesh restart`.

Check with `ollama ps` while a model is loaded.

### 1.6 Open the web app

Open **https://localhost:8765** in Edge or Chrome. The browser warns about the certificate because
it comes from HomeMesh's own certificate authority. Click **Advanced → Continue**, or trust the
certificate once (see INSTALLATION.md, *Trusting the HomeMesh certificate*). Sign in.

### 1.7 Create pairing codes

```powershell
homemesh node pair
```

It prints a one-time code (valid 10 minutes) and a complete `homemesh worker join …` command with
this PC's address and fingerprint. Run it once for **each** Bazzite machine, when you're ready to
paste it there. The web app does the same under **Computers → Add a computer**.

---

## 2. Bazzite desktop (RTX 3080)

Use a normal terminal (Konsole or Ptyxis); no `sudo` is needed anywhere.

### 2.1 Check the NVIDIA setup

```bash
nvidia-smi
```

It should list the RTX 3080 and its memory. If the command isn't found, this machine runs a
non-NVIDIA Bazzite image. Switch to the NVIDIA image following Bazzite's documentation first.

### 2.2 Get HomeMesh

The GitHub CLI handles signing in to the private repository:

```bash
brew install gh
gh auth login                 # choose GitHub.com, HTTPS, log in with a web browser
gh repo clone ladelisandwich/MyAi ~/homemesh -- --branch claude/stoic-bohr-fkoz0p
cd ~/homemesh
```

No `brew`? Use the browser instead. Open the repository on github.com, switch to the branch
`claude/stoic-bohr-fkoz0p`, choose **Code → Download ZIP**, and extract it to `~/homemesh`. The commands
below use `bash …`, so they work from a ZIP too.

### 2.3 Install Ollama (per user, checksum-verified)

```bash
bash deploy/linux/install-ollama.sh
loginctl enable-linger "$(id -un)"   # keep your services running even when nobody is logged in
```

This downloads the official Ollama release and checks its SHA-256 before unpacking anything. It puts
Ollama in `~/.local/share/ollama` and runs it as a **user** service on `127.0.0.1:11434`, so it
can't be reached from the network. Then pull the chat model:

```bash
ollama pull qwen3:8b                 # ~5 GB; tool calling + reasoning; fits the 3080 fully
```

Lighter alternatives are `llama3.1:8b` (~5 GB) and `llama3.2:3b` (~2 GB).

### 2.4 Install HomeMesh and join the hub

```bash
bash deploy/linux/install.sh --no-web    # workers don't need the web app
```

On the Windows PC run `homemesh node pair`, then paste the printed command here, adding a name:

```bash
homemesh worker join --coordinator https://WIN-IP:8765 --code XXXX-XXXX-XXXX --fingerprint 3F:A2:…:9C --name bazzite-3080
homemesh service install
homemesh start
homemesh status
```

`status` should show `coordinator_link: ✓ … reachable`. If join reports a **fingerprint mismatch**,
stop and check the address: something other than your hub answered.

**Gaming:** this worker uses *balanced* mode. While a game keeps the GPU busy (above 85%), it accepts
no new AI work, and it never touches the game. To keep it out of the way entirely, run
`homemesh node pause bazzite-3080` on the hub, or use the Computers page.

---

## 3. Bazzite laptop (RTX 4070 Laptop GPU)

Same as the desktop (2.1–2.4) with these differences:

* Name it `--name bazzite-laptop`.
* Use a model that fits 8 GB comfortably: `ollama pull qwen3:4b` (~2.5 GB) or `llama3.2:3b`.
* Skip `loginctl enable-linger` if you only want HomeMesh while you're logged in.
* **On battery:** suspend and lid-close simply disconnect the worker, and it reconnects when the laptop
  wakes. Polling the NVIDIA GPU every few seconds can stop the laptop's discrete GPU from sleeping,
  which costs battery. When unplugged, stop both services
  (`systemctl --user stop homemesh ollama`) or pause the node from the hub. Start them again with
  `systemctl --user start ollama homemesh`.

---

## 4. Finish on the hub (web app)

1. **Computers:** all three appear online with their GPUs.
2. **Models → Model roles:** Primary assistant → `qwen3:8b`; Embeddings → `nomic-embed-text`.
3. **Chat:** ask something. Each reply shows which model and computer answered, e.g.
   `qwen3:8b · on bazzite-3080 (ollama, gpu)`.
4. **Knowledge** (optional): add a folder *on the computer that has it*. For a folder on a Bazzite
   machine, allow it there first with `homemesh worker allow-path ~/Documents/Notes`, then run
   `homemesh restart`. Folders on the hub itself need no extra step.

## 5. Check that the GPUs are really used

* On a Bazzite machine, during or right after a chat: `ollama ps`. The model should show `100% GPU`.
* On the hub: `homemesh doctor` lists both GPUs. The RX 5700 shows 8 GB total, and its live usage is
  "not reported" because Windows only exposes that for NVIDIA cards. That's expected; HomeMesh then
  schedules by RAM for this PC and says so in each job's decision.
* Benchmarks, in `$HOME\homemesh` on the hub:
  `& "$env:LOCALAPPDATA\Programs\HomeMesh\venv\Scripts\python.exe" scripts\bench.py --runs 3 --model qwen3:8b`
  prints time-to-first-token and tokens/second measured by Ollama.

On the hub, `ollama ps` shows whether Ollama uses a GPU at all. If it says `100% CPU`, check the
`inference compute` lines in `%LOCALAPPDATA%\Ollama\server.log`, update the AMD driver, and see the
optional GPU step in 1.5. The hub works fine as coordinator and embedder either way.

## 6. Phones and other devices

Open `https://WIN-IP:8765`. Trust the HomeMesh certificate on that device (INSTALLATION.md, *Trusting
the HomeMesh certificate*), then use **Add to Home Screen** to install it as an app.

## Updating later

* **Windows:** `cd $HOME\homemesh; git pull; powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -Upgrade`
* **Bazzite:** `cd ~/homemesh && git pull && bash deploy/linux/install.sh --no-web --upgrade`

Upgrade the hub first. Each upgrade stops and restarts HomeMesh, and the database is backed up
automatically before any migration.

## Quick fixes

| Symptom | Fix |
|---|---|
| `homemesh` not found | Open a new terminal (Windows), or add `~/.local/bin` to PATH (Bazzite). |
| Worker can't reach the hub | Check `WIN-IP`, that the network profile is Private, and that the firewall allows Python on Private networks. Try `https://WIN-IP:8765` in a browser on the Bazzite machine. |
| "fingerprint mismatch" | Wrong address, or the hub's certificate authority was recreated. Copy a fresh command from `homemesh node pair`. |
| Chat says "no eligible worker" | Each computer's reason is listed. Usually a model isn't installed on an online computer, or the GPU is busy with a game. |
| Hub's IP changed | Fix the DHCP reservation. Each worker then needs `homemesh worker leave` and a new `worker join`. |

More: [TROUBLESHOOTING.md](../TROUBLESHOOTING.md).
