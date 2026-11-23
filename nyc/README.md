# NYC rent map: RentCast collection and publishing

`collect.py` pulls NYC rental listings from RentCast once per billing period
and writes them in the format Jeff's v3 build reads (`apts-<ts>.txt`). Besides
the five boroughs, it keeps listings in the municipalities outside the city
that lie wholly inside its search circles (`cities/nyc_areas.json`, written by
`nyc/build_areas.py`). It drops room rentals in shared houses, which RentCast
lists under the whole house's bedroom count (under 200 sq ft and $2,000, or 3+
bedrooms at under $400 per bedroom with no square footage), and keeps one
listing per bedrooms, rent, and exact location each month. Changing the areas
or these rules rewrites past months' scrapes from their saved raw responses
and rebuilds the site on the next run. It then
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
| `/srv/nyc-rentmap/secrets/telegram.env` | `root`, 600 | `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_CHAT_ID=...`, for failure alerts |
| `/srv/nyc-rentmap/backup` | `rentmap` | clone of the private backup repository; the container pushes `<period>.tar.xz` archives here |
| `/srv/nyc-rentmap/secrets/backup_key` | `rentmap`, 600 | SSH deploy key with write access to the backup repository only |
| `/srv/nyc-rentmap/site.env` | `root`, 644 | optional: `RENTMAP_GOATCOUNTER_URL=...`, to count visits with GoatCounter |
| `/usr/local/sbin/nyc-rentmap` | `root` | wrapper that starts the container (installed copy of `nyc/deploy/nyc-rentmap`) |
| `/usr/local/sbin/nyc-rentmap-alert` | `root` | sends a Telegram message when a run fails (installed copy of `nyc/deploy/nyc-rentmap-alert`) |
| `/etc/systemd/system/nyc-rentmap.{service,timer}`, `nyc-rentmap-alert.service` | `root` | installed copies of `nyc/deploy/` |

The container runs as `rentmap` with a read-only filesystem, no capabilities,
and only the mounts above. The wrapper and units are copies, so code pulled
from git never runs as root on the host; reinstall them after reviewing any
change to `nyc/deploy/`.

## Setup

Run these on the Pi. Docker must be installed (rootful), and
`command -v docker` must print `/usr/bin/docker`; if it doesn't, change the
path in `nyc/deploy/nyc-rentmap` before installing it. Likewise,
`command -v python3` must print `/usr/bin/python3`, the interpreter the
failure alert script names on its first line. The site build peaks
at about 1.5 GB of memory, so the `available` column of `free -h` should show
at least that.

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
   sudo install -m 755 /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap-alert /usr/local/sbin/
   sudo install -m 644 /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap.service /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap.timer /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap-alert.service /etc/systemd/system/
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

10. Turn on the watchdog. Every day at 12:17 PM Eastern, a GitHub Actions
    workflow (`.github/workflows/nyc-watchdog.yml`, running
    `nyc/watchdog.py`) checks that the current billing period was collected
    and published. From two days after the reset onward, it fails if not,
    and GitHub emails you.

    1. In this repository on GitHub, click **Actions**. If GitHub says
       workflows are turned off for this fork, turn them on there.
    2. In the left sidebar, click **NYC collection watchdog**, click the
       **Run workflow** button, leave the branch dropdown on `master`, and
       click **Run workflow**. The run should succeed; its log starts with
       `OK:`.
    3. In your GitHub notification settings, make sure notifications for
       GitHub Actions are sent by email (failures only is enough). GitHub
       sends scheduled-workflow notices to whoever last changed the schedule
       in the workflow file.

11. Turn on Telegram alerts. When a run fails, systemd starts
    `nyc-rentmap-alert.service`, which messages you on Telegram within
    minutes with the run's last log lines. It runs as a throwaway user that
    can read only the journal and this step's settings file.

    1. Create a bot: in Telegram, open a chat with `@BotFather` (check the
       username is exactly that), send `/newbot`, and give it a name and a
       username ending in `bot`. BotFather replies with the bot's token;
       anyone with it can control the bot, and `/token` in BotFather makes a
       new one.
    2. Send your new bot any message, then get your chat ID from the reply
       to `getUpdates`, run on any computer with `curl` and `python3`
       (zsh's `read` prompt syntax; in bash use `read -rsp "Bot token: " TOKEN`):

       ```bash
       read -rs "TOKEN?Bot token: "
       curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" | python3 -I -c 'import json,sys; print({u["message"]["chat"]["id"] for u in json.load(sys.stdin)["result"] if "message" in u})'
       ```

       It prints your chat ID in braces. If the braces are empty, message the
       bot again and rerun the `curl` line.
    3. Store both on the Pi, readable only by root:

       ```bash
       sudo install -o root -g root -m 600 /dev/null /srv/nyc-rentmap/secrets/telegram.env
       sudoedit /srv/nyc-rentmap/secrets/telegram.env
       ```

       The file holds two lines: `TELEGRAM_BOT_TOKEN=...` and
       `TELEGRAM_CHAT_ID=...`.
    4. Send a test alert:

       ```bash
       sudo systemctl start nyc-rentmap-alert.service
       ```

       A message starting "NYC rent map: alert test." should arrive in your
       chat with the bot. If it doesn't,
       `journalctl -u nyc-rentmap-alert.service -n 20` shows why.

12. Turn on backups. After each collection, the run commits that period's
    raw RentCast responses and run ledger to a private GitHub repository as
    `<period>.tar.xz` (about 0.5 to 1 MB a month). Everything else can be
    rebuilt from them; see "Restoring from the backup". On a Pi set up before
    backups existed, run "Updating the Pi" first, so the installed wrapper
    knows to mount the backup clone.

    1. On GitHub, open the create menu in the upper-right corner of any page
       and click **New repository**. Name it, make it private, leave the
       README, `.gitignore`, and license options off, and click
       **Create repository**.
    2. Create a deploy key for it (GitHub doesn't let one deploy key serve
       two repositories, so this is a second key) and print its public half:

       ```bash
       sudo -u rentmap ssh-keygen -t ed25519 -N "" -C nyc-rentmap-backup -f /srv/nyc-rentmap/secrets/backup_key
       sudo cat /srv/nyc-rentmap/secrets/backup_key.pub
       ```

       In the new private repository: **Settings** → **Deploy Keys** →
       **Add deploy key**. Put `nyc-rentmap Pi backups` in the "Title"
       field, paste the public key in the "Key" field, select
       **Allow write access**, and click **Add key**.
    3. Clone it to `/srv/nyc-rentmap/backup` and configure the clone,
       replacing `OWNER/REPOSITORY` with the new repository and `ADDRESS`
       with the same commit email as in step 3. Cloning an empty repository
       prints a warning; that's expected.

       ```bash
       sudo -u rentmap env GIT_SSH_COMMAND="ssh -i /srv/nyc-rentmap/secrets/backup_key -o UserKnownHostsFile=/srv/nyc-rentmap/secrets/known_hosts -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes" git clone git@github.com:OWNER/REPOSITORY.git /srv/nyc-rentmap/backup
       sudo -u rentmap git -C /srv/nyc-rentmap/backup config core.sshCommand "ssh -i /srv/nyc-rentmap/secrets/backup_key -o UserKnownHostsFile=/srv/nyc-rentmap/secrets/known_hosts -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes"
       sudo -u rentmap git -C /srv/nyc-rentmap/backup config user.name "nyc-rentmap (Pi)"
       sudo -u rentmap git -C /srv/nyc-rentmap/backup config user.email "ADDRESS"
       ```

       The wrapper mounts the clone into the container whenever
       `/srv/nyc-rentmap/backup` exists.
    4. Back up the periods collected so far by running the daily check now.
       Like its scheduled run, it spends RentCast calls only if the current
       period still needs collecting.

       ```bash
       sudo systemctl start nyc-rentmap.service
       sudo nyc-rentmap status
       ```

       `status` should show `backed up:` followed by a commit ID, and the
       private repository should have a `<period>.tar.xz` file for each
       collected period.
13. Optionally, count visits with [GoatCounter](https://www.goatcounter.com/).
    Sign up there; your site's snippet contains a count URL like
    `https://CODE.goatcounter.com/count`. Put it in `site.env`, so it
    stays out of this repository, and republish:

    ```bash
    echo 'RENTMAP_GOATCOUNTER_URL=https://CODE.goatcounter.com/count' | sudo tee /srv/nyc-rentmap/site.env
    sudo nyc-rentmap publish
    ```

    Every build then adds GoatCounter's script to the pages. To stop
    counting, delete `site.env` and run `sudo nyc-rentmap publish` again.

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
   sudo diff -u /usr/local/sbin/nyc-rentmap-alert /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap-alert
   sudo diff -u /etc/systemd/system/nyc-rentmap-alert.service /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap-alert.service
   ```

   No output means that file is unchanged. Lines starting with `+` are what
   step 5 installs; read them before continuing. `No such file or directory`
   for an installed path means the file is new; read the repo copy with
   `sudo cat` followed by its path.

4. Rebuild the image:

   ```bash
   sudo docker build -t nyc-rentmap:latest --build-arg UID="$(id -u rentmap)" --build-arg GID="$(id -g rentmap)" /srv/nyc-rentmap/repo/nyc/docker
   ```

   If `nyc/docker/` didn't change, Docker reuses its cached layers and this
   finishes in seconds.

5. Install the wrapper and systemd units:

   ```bash
   sudo install -m 755 /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap-alert /usr/local/sbin/
   sudo install -m 644 /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap.service /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap.timer /srv/nyc-rentmap/repo/nyc/deploy/nyc-rentmap-alert.service /etc/systemd/system/
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

## Restoring from the backup

If the Pi's data is lost (for example, a failed SD card), rebuild it from
the backup repository. Nothing here spends RentCast calls.

1. On the replacement Pi (or SD card), do Setup steps 1 to 6 and step 12
   parts 2 and 3. Both deploy keys are new, so on GitHub delete the old Pi's
   keys from each repository's **Deploy Keys** settings.
2. Restore the raw responses and run ledgers, then rebuild:

   ```bash
   sudo nyc-rentmap restore
   sudo nyc-rentmap publish
   ```

   `restore` unpacks every archive into `/srv/nyc-rentmap/data` without
   overwriting files already there, and prints how many it wrote.
   `publish` rewrites each collected period's scrape from its raw responses
   and rebuilds and republishes the whole site.
3. Finish with Setup step 7 (check `status`), step 8 (enable the timer), and
   step 11 parts 3 and 4 (Telegram settings and a test alert).

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
- **Backups:** after each collection, the run pushes `<period>.tar.xz` to the
  backup repository and records the commit; `sudo nyc-rentmap status` shows
  it under `backed up:`. A failed backup fails the run (so Telegram alerts
  you) and the next daily check retries it.
- **A Telegram alert** means a run failed; the message carries its last log
  lines, and `journalctl -u nyc-rentmap.service` has the rest. The next daily
  check retries, so one alert can resolve itself; repeated daily alerts mean
  it won't.
- **A watchdog email** means this period's collection or publish didn't
  happen. The failed run's log on GitHub says which: no collection has
  started, collection started but not finished, or collected but not
  published. On the Pi, `journalctl -u nyc-rentmap.service` shows the runs'
  errors and `sudo nyc-rentmap status` shows the period's state. The
  watchdog keeps failing daily until the Pi catches up.
- **A failed run** (non-zero exit, visible in `systemctl --failed`) leaves its
  reservation open, so usage is overstated, never understated; the next daily
  check resumes it.
- **A failed publish** doesn't undo the collection; the next daily check
  retries the build and push without spending any calls. To rebuild and
  republish by hand (for example after changing `cities/nyc.json` or the
  pages), run `sudo nyc-rentmap publish`.
