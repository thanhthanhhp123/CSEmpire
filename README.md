# CSEmpire

Desktop tool for controlling a CSGOEmpire roulette workflow with Playwright and a Tkinter GUI.

## Features

- Opens a dedicated Chromium profile automatically.
- Detects login state from the visible balance.
- Lets you confirm login in the app, then start with the `Farm` button.
- Supports configurable base bet, stop loss, target profit, max rounds, and result timeout.
- Supports side strategies: fixed side, random on reset, or least frequent side in Last 100.
- Waits for the betting window before filling/clicking.
- Shows realtime logs, balance stats, win/loss, and a result chart.

## Setup

Using the existing conda base environment:

```bash
conda activate base
python -m pip install -r requirements.txt
python -m playwright install chromium
```

If `conda` is not available in the shell:

```bash
source ~/miniconda3/bin/activate base
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Run

```bash
conda activate base
python login_csgoempire.py
```

The app will open the browser automatically. If the account is not logged in, log in inside the browser, return to the app, click `Đã đăng nhập`, then click `Farm`.

## Do Not Commit

The following are intentionally ignored because they can contain private cookies, account state, or local machine data:

- `chrome_profile_csgo/`
- `csgoempire_session.json`
- `__pycache__/`

Check before committing:

```bash
git status --short
```

## Push To GitHub

```bash
git init
git remote add origin https://github.com/thanhthanhhp123/CSEmpire.git
git add .gitignore README.md requirements.txt login_csgoempire.py
git commit -m "Initial CSEmpire GUI bot"
git branch -M main
git push -u origin main
```
