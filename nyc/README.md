# NYC rent map: RentCast collection and publishing

`collect.py` pulls NYC rental listings from RentCast once per billing period
and writes them in the format Jeff's v3 build reads (`apts-<ts>.txt`). It then
builds the site with Jeff's `build_rentmap.py` (`RENTMAP_CITY=nyc`, settings in
`cities/nyc.json`) and pushes it to the `gh-pages` branch as a single commit,
which GitHub Pages serves. It runs on a Raspberry Pi in a Docker container,
started daily by a systemd timer; the run does nothing unless the current
billing period still needs collecting or publishing.

Quota safety: RentCast has no hard cap, so `rentcast_quota.json` (committed)
is the only guard. Before any call is sent, the run reserves the calls it
needs in that file, then commits and pushes the reservation. See the
docstrings in `collect.py` and `rentcast_quota.py`.

## Layout on the Pi

| Path | Owner | What |
|---|---|---|
| `/srv/nyc-rentmap/repo` | `rentmap` | clone of this fork; the container runs code from here and pushes quota commits |
| `/srv/nyc-rentmap/data` | `rentmap` | `raw/` gzipped responses (pruned after 24 months), `runs/` local run ledgers, `v3/` scrape files, `site/` the built site |
| `/srv/nyc-rentmap/secrets/deploy_key` | `rentmap`, 600 | SSH deploy key with write access to this repo only |
| `/srv/nyc-rentmap/secrets/known_hosts` | `rentmap`, 644 | GitHub's SSH host keys, copied from `nyc/deploy/known_hosts` |
| `/srv/nyc-rentmap/secrets/rentcast.env` | `root`, 600 | `RENTCAST_API_KEY=...` |
| `/usr/local/sbin/nyc-rentmap` | `root` | wrapper that starts the container (installed copy of `nyc/deploy/nyc-rentmap`) |
| `/etc/systemd/system/nyc-rentmap.{service,timer}` | `root` | installed copies of `nyc/deploy/` |

The container runs as `rentmap` with a read-only filesystem, no capabilities,
and only the mounts above. The wrapper and units are copies, so code pulled
from git never runs as root on the host; reinstall them after reviewing any
change to `nyc/deploy/`.

## Setup

Run these on the Pi. Docker must be installed (rootful), and
`command -v docker` must print `/usr/bin/docker`; if it doesn't, change the
path in `nyc/deploy/nyc-rentmap` before installing it.

1. Create the user and directories:

   ```bash
   sudo useradd --system --home-dir /srv/nyc-rentmap --no-create-home --shell /usr/sbin/nologin rentmap
   sudo install -d -o rentmap -g rentmap -m 750 /srv/nyc-rentmap /srv/nyc-rentmap/data
   sudo install -d -o rentmap -g rentmap -m 700 /srv/nyc-rentmap/secrets
   ```

2. Create the deploy key and print its public half:

   ```bash
   sudo -u rentmap ssh-keygen -t ed25519 -N "" -C nyc-rentmap-pi -f /srv/nyc-rentmap/secrets/deploy_key
   sudo cat /srv/nyc-rentmap/secrets/deploy_key.pub
   ```

   On GitHub, in this repository: **Settings** → **Deploy Keys** →
   **Add deploy key**. Put `nyc-rentmap Pi` in the "Title" field, paste the
   public key in the "Key" field, select **Allow write access**, and click
   **Add key**.

3. Clone over HTTPS (the repo is public), then install the pinned host keys
   and switch the remote to SSH with the deploy key:

   ```bash
   sudo -u rentmap git clone https://github.com/jschnurholmes/apartment_prices.git /srv/nyc-rentmap/repo
   sudo install -o rentmap -g rentmap -m 644 /srv/nyc-rentmap/repo/nyc/deploy/known_hosts /srv/nyc-rentmap/secrets/known_hosts
   sudo ssh-keygen -lf /srv/nyc-rentmap/secrets/known_hosts
   ```

   The fingerprints must be exactly these, which match GitHub's published
   list at
   https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints:

   ```
   SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU (ED25519)
   SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM (ECDSA)
   SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s (RSA)
   ```

   Then configure the clone. The commit email is published in the repo's
   history, so use an address you're comfortable showing:

   ```bash
   sudo -u rentmap git -C /srv/nyc-rentmap/repo remote set-url origin git@github.com:jschnurholmes/apartment_prices.git
   sudo -u rentmap git -C /srv/nyc-rentmap/repo config core.sshCommand "ssh -i /srv/nyc-rentmap/secrets/deploy_key -o UserKnownHostsFile=/srv/nyc-rentmap/secrets/known_hosts -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes"
   sudo -u rentmap git -C /srv/nyc-rentmap/repo config user.name "nyc-rentmap (Pi)"
   sudo -u rentmap git -C /srv/nyc-rentmap/repo config user.email "ADDRESS"
   sudo -u rentmap git -C /srv/nyc-rentmap/repo fetch origin
   ```

   The `fetch` succeeding confirms the deploy key and host keys work.

4. Store the API key, readable only by root (Docker reads it when starting
   the container):

   ```bash
   sudo install -o root -g root -m 600 /dev/null /srv/nyc-rentmap/secrets/rentcast.env
   sudoedit /srv/nyc-rentmap/secrets/rentcast.env
   ```

   The file holds one line: `RENTCAST_API_KEY=...`.

5. Build the image, with the container user matching `rentmap`'s IDs:

   ```bash
   sudo docker build -t nyc-rentmap:latest --build-arg UID="$(id -u rentmap)" --build-arg GID="$(id -g rentmap)" /srv/nyc-rentmap/repo/nyc/docker
   ```

6. Install the wrapper and systemd units:

   ```bash
   sudo install -m 755 /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap /usr/local/sbin/nyc-rentmap
   sudo install -m 644 /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap.service /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

7. Check everything without spending any calls:

   ```bash
   sudo nyc-rentmap status
   ```

   It should print the billing period, the quota (matching the RentCast
   dashboard), and "no run yet this period".

8. Enable the timer. **This starts real collection:** the next daily check
   runs a collection if the period is due, spending RentCast calls.

   ```bash
   sudo systemctl enable --now nyc-rentmap.timer
   ```

   Check the schedule with `systemctl list-timers nyc-rentmap.timer` and the
   runs with `journalctl -u nyc-rentmap.service`.

9. After the first run has published (the `gh-pages` branch exists on
   GitHub), turn on GitHub Pages once: in this repository, **Settings** →
   **Pages** → under "Build and deployment", set "Source" to
   **Deploy from a branch**, choose the `gh-pages` branch and the "/(root)"
   folder, and click **Save**. The site is then at
   https://jschnurholmes.github.io/apartment_prices/.

## Upgrading a Pi set up before publishing existed

Publishing added numpy, scipy, and pillow to the image and a `publish` command
to the wrapper. On a Pi that already completed the setup above, rebuild the
image (step 5) and reinstall the wrapper and units (step 6), then do step 9
after the next run.

The site build peaked at about 1.5 GB of memory in testing (2000-pixel map).
Check the Pi has that much free with `free -h`; a 1 or 2 GB Pi may fail or
swap heavily during the build.

## Day to day

- **Code updates** arrive by `git pull` at the start of each run. Changes to
  `nyc/deploy/` need reinstalling (step 6); changes to `nyc/docker/` need a
  rebuild (step 5).
- **Manual RentCast calls** made anywhere must be recorded in the committed
  tracker: pull, run `python3 -I nyc/rentcast_quota.py add-usage N --note "..."`,
  commit, and push. To match the dashboard exactly, use
  `set-usage-before-run N` instead.
- **A failed run** (non-zero exit, visible in `systemctl --failed`) leaves its
  reservation open, so usage is overstated, never understated; the next daily
  check resumes it.
- **A failed publish** doesn't undo the collection; the next daily check
  retries the build and push without spending any calls. To rebuild and
  republish by hand (for example after changing `cities/nyc.json` or the
  pages), run `sudo nyc-rentmap publish`.
