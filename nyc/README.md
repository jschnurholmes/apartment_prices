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

8. Enable the timer. **This starts real collection:** the daily check runs
   between 9:00 and 9:30 AM Eastern (10:00 to 10:30 AM during daylight saving
   time), and the next one collects if the period is due, spending RentCast
   calls.

   ```bash
   sudo systemctl enable --now nyc-rentmap.timer
   ```

   Check the schedule with `systemctl list-timers nyc-rentmap.timer` and the
   runs with `journalctl -u nyc-rentmap.service`.

9. After the first run has published (the `gh-pages` branch exists on
   GitHub), turn on GitHub Pages once. In this repository on GitHub, click
   **Settings**, then **Pages** in the sidebar. Under "Build and deployment",
   set "Source" to **Deploy from a branch**, choose `gh-pages` in the branch
   dropdown, set the folder dropdown to the root of the branch (not `/docs`),
   and click **Save**.

   To confirm the site deployed, open the repository's **Actions** tab on
   GitHub. Every push to `gh-pages` starts a Pages deployment run there; a
   failed run means the site wasn't updated.

## Updating the Pi

Follow this after pushing any change to `master` that the Pi should run. Run
every step every time: a daily run may already have pulled the new code, so
the pull alone can't tell you whether the image or the installed files are out
of date. Each step is safe to repeat.

1. Pause the daily check and make sure no run is in progress:

   ```bash
   sudo systemctl stop nyc-rentmap.timer
   systemctl is-active nyc-rentmap.service
   ```

   The second command prints `activating` while a run is in progress. Repeat
   it every minute or so until it prints `inactive` or `failed` (`failed` only
   means the last run failed). Runs start between 9:00 and 9:30 AM Eastern
   (10:00 to 10:30 AM during daylight saving time) and take a few minutes.
   Stopping the timer doesn't cut off a run in progress, and a check that
   comes due while the timer is paused runs as soon as step 7 restarts it.

2. Pull the new code and confirm the clone has it:

   ```bash
   sudo -u rentmap git -C /srv/nyc-rentmap/repo pull --ff-only
   sudo -u rentmap git -C /srv/nyc-rentmap/repo log -1 --oneline
   ```

   The second command must show the newest commit at
   https://github.com/jschnurholmes/apartment_prices/commits/master. If the
   pull fails, follow "If the pull fails" below, then repeat this step.

3. Compare the files that run as root with the installed copies:

   ```bash
   sudo diff -u /usr/local/sbin/nyc-rentmap /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap
   sudo diff -u /etc/systemd/system/nyc-rentmap.service /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap.service
   sudo diff -u /etc/systemd/system/nyc-rentmap.timer /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap.timer
   ```

   No output means that file is unchanged. Lines starting with `+` are what
   step 5 installs; read them before continuing.

4. Rebuild the image:

   ```bash
   sudo docker build -t nyc-rentmap:latest --build-arg UID="$(id -u rentmap)" --build-arg GID="$(id -g rentmap)" /srv/nyc-rentmap/repo/nyc/docker
   ```

   If `nyc/docker/` didn't change, Docker reuses its cached layers and this
   finishes in seconds.

5. Install the wrapper and systemd units:

   ```bash
   sudo install -m 755 /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap /usr/local/sbin/nyc-rentmap
   sudo install -m 644 /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap.service /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

6. If the update changes the site (`cities/`, `site/`, or the build code),
   republish now; otherwise skip this, and the site is rebuilt after the next
   collection. This spends no RentCast calls:

   ```bash
   sudo nyc-rentmap publish
   ```

   It prints `gh-pages unchanged` if the rebuilt site is identical to the
   published one. If it prints `gh-pages updated to ...`, open the
   repository's **Actions** tab on GitHub and check that the newest Pages
   deployment run succeeded.

7. Resume the daily check and confirm the state:

   ```bash
   sudo systemctl start nyc-rentmap.timer
   systemctl list-timers nyc-rentmap.timer
   sudo nyc-rentmap status
   ```

   `list-timers` shows the next check under `NEXT`, in the Pi's system time
   zone (`timedatectl` shows which). `status` spends no calls; it prints the
   billing period, the quota, this period's passes, and the `published`
   commit.

### If the pull fails

The timer stays paused (step 1) while you do this. What to do depends on the
error `pull` printed.

**`Could not resolve host`, `Permission denied (publickey)`, or
`Host key verification failed`:** the Pi can't reach GitHub, or GitHub
rejected the deploy key, or the host keys don't match. Check the Pi's network
connection and that the deploy key is still listed, with write access, in this
repository's deploy key settings on GitHub. For a host key error, compare the
fingerprints from `sudo ssh-keygen -lf /srv/nyc-rentmap/secrets/known_hosts`
with
https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints.
Then repeat step 2.

**`Not possible to fast-forward`, `divergent branches`, or a message that
local changes would be overwritten:** the clone has quota changes from a run
that didn't reach GitHub. See what they are:

```bash
sudo -u rentmap git -C /srv/nyc-rentmap/repo fetch origin
sudo -u rentmap git -C /srv/nyc-rentmap/repo status --short
sudo -u rentmap git -C /srv/nyc-rentmap/repo log --oneline origin/master..HEAD
```

1. `status --short` lists uncommitted changes. Runs only ever change
   `nyc/rentcast_quota.json`; if it's listed, commit it the way the next run
   would:

   ```bash
   sudo -u rentmap git -C /srv/nyc-rentmap/repo commit -m "Commit quota changes left by an interrupted run" -- nyc/rentcast_quota.json
   ```

   Any other file listed was edited by hand in the clone. The rebase below
   refuses to run until it's gone; once you know the edit isn't needed,
   discard it with
   `sudo -u rentmap git -C /srv/nyc-rentmap/repo checkout -- PATH`, replacing
   `PATH` with the listed path.

2. `log` lists commits the Pi has and GitHub doesn't. They should all be
   quota commits ("Reserve ...", "Close ...", or "Commit quota changes ...").
   Move them on top of GitHub's commits and push them:

   ```bash
   sudo -u rentmap git -C /srv/nyc-rentmap/repo rebase origin/master
   sudo -u rentmap git -C /srv/nyc-rentmap/repo push origin HEAD:master
   ```

   Then repeat step 2.

3. If the rebase stops with
   `CONFLICT (content): Merge conflict in nyc/rentcast_quota.json`, the quota
   tracker was changed both on the Pi and elsewhere (usually manual usage
   recorded from another clone). Open the file as `rentmap`:

   ```bash
   sudoedit -u rentmap /srv/nyc-rentmap/repo/nyc/rentcast_quota.json
   ```

   Delete the `<<<<<<<`, `=======`, and `>>>>>>>` lines and keep the entries
   from both sides, so `external_usage` and `reservations` each list every
   entry, with a comma between entries. Check the file is valid JSON (no
   output means it is), then finish the rebase and push:

   ```bash
   sudo -u rentmap python3 -I -m json.tool /srv/nyc-rentmap/repo/nyc/rentcast_quota.json > /dev/null
   sudo -u rentmap git -C /srv/nyc-rentmap/repo add nyc/rentcast_quota.json
   sudo -u rentmap env GIT_EDITOR=true git -C /srv/nyc-rentmap/repo rebase --continue
   sudo -u rentmap git -C /srv/nyc-rentmap/repo push origin HEAD:master
   ```

   If the rebase stops again on a later commit, repeat this for each stop.
   Then repeat step 2.

To back out of a rebase at any point, run
`sudo -u rentmap git -C /srv/nyc-rentmap/repo rebase --abort`, and resume the
timer with `sudo systemctl start nyc-rentmap.timer`.

### First update after publishing was added

A Pi set up before publishing existed needs three extra things, because
publishing added numpy, scipy, and pillow to the image, a `publish` command to
the wrapper, and the `gh-pages` branch:

1. Before starting, check memory with `free -h`. The site build peaked at
   about 1.5 GB in testing (2000-pixel map), so the `available` column should
   show at least that; a 1 or 2 GB Pi may fail or swap heavily.
2. Don't skip step 6 above: run `sudo nyc-rentmap publish` to build the site
   from the data already collected and create the `gh-pages` branch. Step 7's
   `status` should then show `published:` followed by a commit ID.
3. After step 7, turn on GitHub Pages once. In this repository on GitHub,
   click **Settings**, then **Pages** in the sidebar. Under
   "Build and deployment", set "Source" to **Deploy from a branch**, choose
   `gh-pages` in the branch dropdown, set the folder dropdown to the root of
   the branch (not `/docs`), and click **Save**. Then open the repository's
   **Actions** tab and check that the Pages deployment run succeeded.

## Day to day

- **Code updates:** each run pulls new commits and restarts onto them, so
  changes to the Python code and settings take effect in that same run.
  Changes to `nyc/deploy/` and `nyc/docker/` take effect only through
  "Updating the Pi" above.
- **Manual RentCast calls** made anywhere other than the Pi's runs must be
  recorded in the committed tracker before the Pi's next run; otherwise the
  run believes more quota is left than there is. From the top folder of a
  clone of this repo on another computer, outside the daily check's window:

  ```bash
  git pull --ff-only
  python3 -I nyc/rentcast_quota.py add-usage N --note "what the calls were for"
  git commit -m "Record N manual RentCast calls" -- nyc/rentcast_quota.json
  git push
  ```

  Replace `N` with the number of calls, in both places. Without
  `--date YYYY-MM-DD`, the calls are recorded as made today. If the push is
  rejected because the Pi pushed in the meantime, run `git pull --rebase` and
  `git push` again.

  To make the tracker match the RentCast dashboard instead, run
  `python3 -I nyc/rentcast_quota.py status` and subtract its
  "used by monthly run" figure from the dashboard's count. Record the result
  with `set-usage-before-run`, which replaces this period's manual usage
  rather than adding to it, then commit and push as above:

  ```bash
  python3 -I nyc/rentcast_quota.py set-usage-before-run N --note "match dashboard"
  ```
- **A failed run** (non-zero exit, visible in `systemctl --failed`) leaves its
  reservation open, so usage is overstated, never understated; the next daily
  check resumes it.
- **A failed publish** doesn't undo the collection; the next daily check
  retries the build and push without spending any calls. To rebuild and
  republish by hand (for example after changing `cities/nyc.json` or the
  pages), run `sudo nyc-rentmap publish`.
