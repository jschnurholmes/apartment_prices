#!/bin/bash
set -u
T=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$T/../.." && pwd)
if [ -n "${PYTHON:-}" ]; then PY=$PYTHON
elif [ -x "$REPO/nyc/.venv/bin/python" ]; then PY=$REPO/nyc/.venv/bin/python
else PY=python3; fi
W=$(mktemp -d)
PASS=0; FAIL=0
check() { if eval "$2"; then echo "PASS $1"; PASS=$((PASS+1)); else echo "FAIL $1"; FAIL=$((FAIL+1)); fi; }

setup() {
  rm -rf "$W"; mkdir -p "$W/seed" "$W/data"
  git init -q --bare -b master "$W/origin.git"
  (cd "$REPO" && git ls-files -z --cached --others --exclude-standard | xargs -0 tar cf -) | tar xf - -C "$W/seed"
  python3 -I -c "import json,sys; p=sys.argv[1]; d=json.load(open(p)); d['reservations']=[]; json.dump(d, open(p,'w'), indent=2)" "$W/seed/nyc/rentcast_quota.json"
  git -C "$W/seed" init -q -b master
  git -C "$W/seed" -c user.name=t -c user.email=t@t add -A
  git -C "$W/seed" -c user.name=t -c user.email=t@t commit -q -m seed
  git -C "$W/seed" remote add origin "$W/origin.git"
  git -C "$W/seed" push -q origin master
  for c in server laptop; do
    git clone -q "$W/origin.git" "$W/$c"
    git -C "$W/$c" config user.name "$c"; git -C "$W/$c" config user.email "$c@test"
  done
}
run() { "$PY" -I "$T/scenario.py" "$W/server" "$W/data" "$1" "$2" 2>&1; }
pages_commits() { git -C "$W/origin.git" rev-list --count gh-pages 2>/dev/null || echo 0; }
pages_file() { git -C "$W/origin.git" show "gh-pages:$1" 2>/dev/null; }
calls() { [ -f "$W/data/fake_calls.log" ] && wc -l < "$W/data/fake_calls.log" | tr -d ' ' || echo 0; }
q() { python3 -I -c "
import json,sys; sys.path.insert(0,'$W/$1/nyc'); import rentcast_quota as r, datetime
x=r.Quota('$W/$1/nyc/rentcast_quota.json'); d=datetime.date(2026,11,22)
res=x.reservation('run-2026-11-21') or {}
print(x.used(d), res.get('status'), res.get('reserved'), res.get('spent'))"; }
origin_q() { git -C "$W/laptop" pull -q --ff-only; q laptop; }
origin_qd() { git -C "$W/laptop" pull -q --ff-only; python3 -I -c "
import sys; sys.path.insert(0,'$W/laptop/nyc'); import rentcast_quota as r, datetime
x=r.Quota('$W/laptop/nyc/rentcast_quota.json'); d=datetime.date.fromisoformat('$1')
res=x.reservation('$2') or {}
print(x.used(d), res.get('status'), res.get('reserved'), res.get('spent'))"; }
add_cname() {
  local blob tree c
  blob=$(printf 'maps.example.test' | git -C "$W/origin.git" hash-object -w --stdin)
  GIT_INDEX_FILE="$W/cname.idx" git -C "$W/origin.git" read-tree gh-pages
  GIT_INDEX_FILE="$W/cname.idx" git -C "$W/origin.git" update-index --add --cacheinfo "100644,$blob,CNAME"
  tree=$(GIT_INDEX_FILE="$W/cname.idx" git -C "$W/origin.git" write-tree); rm -f "$W/cname.idx"
  c=$(git -C "$W/origin.git" -c user.name=gh -c user.email=gh@test commit-tree "$tree" -p gh-pages -m "Create CNAME")
  git -C "$W/origin.git" update-ref refs/heads/gh-pages "$c"
}
wd() { git -C "$W/laptop" pull -q --ff-only; git -C "$W/laptop" fetch -q origin; python3 -I "$W/laptop/nyc/watchdog.py" --today "$1" 2>&1; echo "exit=$?"; }
manual() { (cd "$W/laptop" && git pull -q --ff-only && python3 -I nyc/rentcast_quota.py add-usage "$1" --date "$2" --note test >/dev/null && git commit -qam "manual usage" && git push -q); }

g_publish() {
echo "=== publish: normal run, then rerun ==="
setup
out=$(run normal 2026-11-22); echo "$out" | grep -E 'RESULT|spent|wrote'
check "23 calls made (14+1+7+1)" '[ "$(calls)" = 23 ]'
check "origin quota: used 32, reservation closed, spent 23" '[ "$(origin_q)" = "32 closed 23 23" ]'
check "origin has reserve and close commits" 'git -C "$W/origin.git" log --oneline | grep -q "Close RentCast reservation"'
n=$(ls "$W"/data/v3/apts-*.txt | wc -l | tr -d " "); lines=$(grep -c "^[0-9]" "$W"/data/v3/apts-*.txt)
check "one v3 scrape file, NYC-only listings (8,056 of 10,072 synthetic)" '[ "$n" = 1 ] && [ "$lines" = 8056 ]'
check "v3 ids contain no whitespace" '! grep "^[0-9]" "$W"/data/v3/apts-*.txt | awk "NF!=5" | grep -q .'
check "raw responses gzipped on disk (23 files)" '[ "$(ls "$W"/data/raw/2026-11-21/*.json.gz | wc -l | tr -d " ")" = 23 ]'
check "gh-pages published as one parentless commit" '[ "$(pages_commits)" = 1 ]'
check "gh-pages has NYC city.json, a month map, pages, .nojekyll" 'pages_file city.json | grep -q "\"name\": \"NYC\"" && git -C "$W/origin.git" ls-tree --name-only gh-pages | grep -q "^rentmap\..*\.png$" && pages_file index.html >/dev/null && pages_file trends.html >/dev/null && git -C "$W/origin.git" ls-tree --name-only gh-pages | grep -qx ".nojekyll"'
check "published pages are titled NYC" 'pages_file index.html | grep -q "<title>NYC Rent Map</title>" && pages_file trends.html | grep -q "<title>NYC Rent Trends</title>"'
check "published pages otherwise match the originals byte for byte" 'pages_file index.html | sed "s/<title>NYC /<title>Boston /" | cmp -s - "$REPO/site/index.html" && pages_file trends.html | sed "s/<title>NYC /<title>Boston /" | cmp -s - "$REPO/site/trends.html"'
check "ledger records the publish" 'grep -q "\"published\": \"[0-9a-f]\{40\}\"" "$W"/data/runs/2026-11-21.json'
add_cname
before=$(git -C "$W/origin.git" rev-parse gh-pages)
out=$(run normal 2026-11-25); echo "$out" | grep RESULT
check "rerun same period makes no calls" '[ "$(calls)" = 23 ] && echo "$out" | grep -q "already collected"'
check "rerun does not republish" '[ "$(git -C "$W/origin.git" rev-parse gh-pages)" = "$before" ]'
out=$(run publish 2026-11-25); echo "$out" | grep -E "RESULT|gh-pages"
check "manual publish with nothing changed is skipped" 'echo "$out" | grep -q "gh-pages unchanged" && [ "$(git -C "$W/origin.git" rev-parse gh-pages)" = "$before" ]'
out=$(wd 2026-11-23); echo "$out"
check "watchdog passes a collected and published period, past GitHub's CNAME commit" 'echo "$out" | grep -q "^OK: period 2026-11-21: collected (23 calls) and published" && echo "$out" | grep -q "exit=0"'
check "CNAME added on GitHub is still published" '[ "$(pages_file CNAME)" = "maps.example.test" ]'

echo "=== publish: GitHub unreachable at publish: nothing pushed ==="
mv "$W/origin.git" "$W/origin.offline"
out=$(run publish 2026-11-25); echo "$out" | grep RESULT
mv "$W/origin.offline" "$W/origin.git"
check "publish stops when gh-pages can't be read" 'echo "$out" | grep -q "RESULT stopped: git ls-remote failed"'
check "gh-pages and its CNAME untouched" '[ "$(git -C "$W/origin.git" rev-parse gh-pages)" = "$before" ] && [ "$(pages_file CNAME)" = "maps.example.test" ]'

echo "=== publish: build failure: collection kept, publish retried next day ==="
setup
mv "$W/server/cities/nyc_housing_mask.png" "$W/mask.bak"
out=$(run normal 2026-11-22); echo "$out" | grep -E "RESULT|spent"
check "collection closed despite build failure" '[ "$(calls)" = 23 ] && [ "$(origin_q)" = "32 closed 23 23" ] && echo "$out" | grep -q "RESULT stopped: site build failed"'
out=$(wd 2026-11-23); echo "$out"
check "watchdog fails a collected but unpublished period" 'echo "$out" | grep -q "^FAIL: period 2026-11-21: collected (23 calls) but not published since" && echo "$out" | grep -q "exit=1"'
check "nothing published, ledger not marked" '[ "$(pages_commits)" = 0 ] && ! grep -q "\"published\"" "$W"/data/runs/2026-11-21.json'
mv "$W/mask.bak" "$W/server/cities/nyc_housing_mask.png"
out=$(run normal 2026-11-23); echo "$out" | grep -E "RESULT|already|gh-pages"
check "next day publishes without collecting again" '[ "$(calls)" = 23 ] && [ "$(pages_commits)" = 1 ] && echo "$out" | grep -q "already collected"'
out=$(wd 2026-11-24); echo "$out"
check "watchdog passes once the retry published" 'echo "$out" | grep -q "^OK: period 2026-11-21: collected" && echo "$out" | grep -q "exit=0"'

}

g_quota() {
echo "=== quota: reset day waits ==="
setup
out=$(run normal 2026-11-21); echo "$out" | grep -E 'RESULT|waiting'
check "no calls on reset day" '[ "$(calls)" = 0 ] && echo "$out" | grep -q waiting'
out=$(wd 2026-11-22); echo "$out"
check "watchdog doesn't check before two days after the reset" 'echo "$out" | grep -q "^OK: period 2026-11-21: not checked until 2026-11-23" && echo "$out" | grep -q "exit=0"'
out=$(wd 2026-11-23); echo "$out"
check "watchdog fails when no collection started" 'echo "$out" | grep -q "^FAIL: period 2026-11-21: no collection has started" && echo "$out" | grep -q "exit=1"'

echo "=== quota: tight quota: priority decides ==="
setup
(cd "$W/laptop" && python3 -I nyc/rentcast_quota.py add-usage 24 --date 2026-11-22 --note test >/dev/null && git commit -qam "manual usage" && git push -q)
out=$(run normal 2026-11-22); echo "$out" | grep -E 'RESULT|active|inactive|spent'
check "Active 4-boro and Active SI complete; Inactive 4-boro incomplete; Inactive SI skipped" \
  'echo "$out" | grep -q "active-4boro *complete" && echo "$out" | grep -q "active-si *complete" && echo "$out" | grep -q "inactive-4boro *incomplete" && echo "$out" | grep -q "inactive-si *skipped"'
check "16 calls made, used 49 of 50 (1 kept free)" '[ "$(calls)" = 16 ] && [ "$(origin_q)" = "49 closed 16 16" ]'

echo "=== quota: Active 4-boro alone exceeds quota: it gets everything ==="
setup
manual 33 2026-11-22
out=$(run normal 2026-11-22); echo "$out" | grep -E 'RESULT|active|inactive|spent'
check "Active 4-boro spends all 7 available calls, incomplete" 'echo "$out" | grep -q "active-4boro *incomplete total=6557 pages=14" && [ "$(calls)" = 7 ]'
check "all other passes skipped" 'echo "$out" | grep -q "active-si *skipped" && echo "$out" | grep -q "inactive-4boro *skipped" && echo "$out" | grep -q "inactive-si *skipped"'
check "used 49 of 50, closed with 7 spent" '[ "$(origin_q)" = "49 closed 7 7" ]'

echo "=== quota: Inactive 4-boro won't fit: skipped by estimate, SI passes still run ==="
setup
out=$(run normal 2026-11-22); echo "$out" | grep RESULT
add_cname
manual 33 2026-12-22
out=$(run normal 2026-12-22); echo "$out" | grep -E 'RESULT|active|inactive|spent|need'
check "Inactive 4-boro skipped from last period's estimate, no probe call" 'echo "$out" | grep -q "inactive-4boro *skipped *total=None" && echo "$out" | grep -q "inactive-4boro: last period.s 3029 listings need 7 pages; 1 calls available"'
check "Active passes and Inactive SI complete" 'echo "$out" | grep -q "active-4boro *complete" && echo "$out" | grep -q "active-si *complete" && echo "$out" | grep -q "inactive-si *complete"'
check "December run made 16 calls (14 + 1 + 0 + 1)" '[ "$(calls)" = 39 ]'
check "December quota: used 49, closed with 16 spent" '[ "$(origin_qd 2026-12-22 run-2026-12-21)" = "49 closed 16 16" ]'
check "December run published, site still a single commit" 'echo "$out" | grep -q "RESULT ok" && [ "$(pages_commits)" = 1 ] && grep -q "\"published\": \"[0-9a-f]\{40\}\"" "$W"/data/runs/2026-12-21.json'
check "December publish carried the CNAME over" '[ "$(pages_file CNAME)" = "maps.example.test" ] && pages_file city.json | grep -q "\"name\": \"NYC\""'

echo "=== quota: manual usage pushed from laptop is respected ==="
setup
(cd "$W/laptop" && python3 -I nyc/rentcast_quota.py add-usage 40 --date 2026-11-22 --note test >/dev/null && git commit -qam "manual usage" && git push -q)
out=$(run normal 2026-11-22); echo "$out" | grep -E 'RESULT|quota'
check "no calls when only 0 remain after keep_free" '[ "$(calls)" = 0 ]'

}

g_failures() {
echo "=== failures: crash mid-pass, then resume ==="
setup
out=$(run crash@6 2026-11-22); echo "$out" | grep RESULT
check "crash left call 6 marked sending" 'grep -q "\"sending\"" "$W"/data/runs/2026-11-21.json'
check "origin reservation open, 14 reserved" '[ "$(origin_q)" = "23 open 14 0" ]'
out=$(wd 2026-11-23); echo "$out"
check "watchdog fails an unfinished collection" 'echo "$out" | grep -q "^FAIL: period 2026-11-21: collection started but not finished (0 of 14 calls spent)" && echo "$out" | grep -q "exit=1"'
out=$(run normal 2026-11-22); echo "$out" | grep -E 'RESULT|active-4boro|spent'
check "resume: lost page counted, not re-sent (no room), pass incomplete" 'echo "$out" | grep -q "active-4boro *incomplete"'
check "total calls 23 including the lost one" '[ "$(calls)" = 23 ]'
check "origin: closed with 23 spent, used 32" '[ "$(origin_q)" = "32 closed 23 23" ]'
check "never reserved more than 23" '! git -C "$W/origin.git" log --format=%s | grep -E "Reserve (2[4-9]|[3-9][0-9])" | grep -q .'

echo "=== failures: HTTP 403 stops the run, unbilled; retry resumes ==="
setup
out=$(run http403 2026-11-22); echo "$out" | grep RESULT
check "stopped after 1 unbilled call, reservation left open" '[ "$(calls)" = 1 ] && [ "$(origin_q)" = "10 open 1 0" ]'
out=$(run normal 2026-11-23); echo "$out" | grep -E 'RESULT|spent'
check "retry completes: 23 billed, 24 calls sent" '[ "$(calls)" = 24 ] && [ "$(origin_q)" = "32 closed 23 23" ]'

echo "=== failures: GitHub unreachable: nothing spent ==="
setup
mv "$W/origin.git" "$W/origin.offline"
out=$(run normal 2026-11-22); echo "$out" | grep RESULT
check "no calls when origin is unreachable" '[ "$(calls)" = 0 ] && echo "$out" | grep -q "RESULT stopped"'

}

g_restart() {
echo "=== restart: code pushed during the period runs in the same run ==="
setup
push_marker() {
  (cd "$W/laptop" && git pull -q --ff-only && python3 -I - "$1" "$2" "$3" <<'PY' && git commit -qam "marker $2" && git push -q)
import sys
anchor, text, indent = sys.argv[1], sys.argv[2], int(sys.argv[3])
p = "nyc/collect.py"; s = open(p).read()
assert anchor in s
open(p, "w").write(s.replace(anchor, anchor + "\n" + " " * indent + "print(%r)" % text, 1))
PY
}
push_marker "    def run(self):" "NEW RUN CODE" 8
out=$(run normal 2026-11-22); echo "$out" | grep -E "RESULT|restarting|NEW RUN CODE"
check "run restarted once after pulling, and ran the new code" '[ "$(echo "$out" | grep -c "restarting")" = 1 ] && [ "$(echo "$out" | grep -c "NEW RUN CODE")" = 1 ]'
check "restarted run completed normally" 'echo "$out" | grep -q "RESULT ok" && [ "$(calls)" = 23 ] && [ "$(origin_q)" = "32 closed 23 23" ] && [ "$(pages_commits)" = 1 ]'
push_marker "def build_and_publish(repo, data_dir, git, label):" "NEW PUBLISH CODE" 4
out=$(run mainpublish 2026-11-25); echo "$out" | grep -E "RESULT|restarting|NEW PUBLISH CODE"
check "manual publish restarted once and ran the new code" '[ "$(echo "$out" | grep -c "restarting")" = 1 ] && [ "$(echo "$out" | grep -c "NEW PUBLISH CODE")" = 1 ] && echo "$out" | grep -q "RESULT ok"'
out=$(run normal 2026-11-26); echo "$out" | grep -E "RESULT|restarting|already"
check "no restart when nothing new was pulled" '! echo "$out" | grep -q "restarting" && echo "$out" | grep -q "already collected"'

}

g_alert() {
echo "=== alert: Telegram alert script ==="
out=$("$PY" -I "$T/test_alert.py" 2>&1); echo "$out"
PASS=$((PASS + $(echo "$out" | grep -c '^PASS')))
FAIL=$((FAIL + $(echo "$out" | grep -c '^FAIL')))
echo "$out" | grep -q '^alert tests done' || { echo "FAIL alert tests didn't finish"; FAIL=$((FAIL + 1)); }

}

g_areas() {
echo "=== areas: area filter on real boundaries ==="
out=$("$PY" -I "$T/test_areas.py" 2>&1); echo "$out"
PASS=$((PASS + $(echo "$out" | grep -c '^PASS')))
FAIL=$((FAIL + $(echo "$out" | grep -c '^FAIL')))
echo "$out" | grep -q '^area tests done' || { echo "FAIL area tests didn't finish"; FAIL=$((FAIL + 1)); }

}

g_scrape() {
echo "=== scrape: scrape writer: room rentals and same-month duplicates ==="
out=$("$PY" -I "$T/test_scrape.py" 2>&1); echo "$out"
PASS=$((PASS + $(echo "$out" | grep -c '^PASS')))
FAIL=$((FAIL + $(echo "$out" | grep -c '^FAIL')))
echo "$out" | grep -q '^scrape tests done' || { echo "FAIL scrape tests didn't finish"; FAIL=$((FAIL + 1)); }

}

g_refilter() {
echo "=== refilter: areas change mid-period: scrape rewritten from raw, site rebuilt ==="
setup
out=$(run normal 2026-11-22); echo "$out" | grep -E "RESULT|rebuilding|wrote"
check "first publish forces a full build" 'echo "$out" | grep -q "rebuilding every month"'
before=$(git -C "$W/origin.git" rev-parse gh-pages)
(cd "$W/laptop" && git pull -q --ff-only && python3 -I - <<'PY' && git commit -qam "test area" && git push -q)
import json
p = "cities/nyc_areas.json"
d = json.load(open(p))
d["areas"].append({"geoid": "test", "name": "Test Area", "state": "NJ", "housing_units": 0, "label": [40.655, -73.95],
                   "rings": [[[-73.96, 40.64], [-73.94, 40.64], [-73.94, 40.67], [-73.96, 40.67]]]})
json.dump(d, open(p, "w"))
PY
out=$(run normal 2026-11-23); echo "$out" | grep -E "RESULT|restarting|wrote|rebuilding|gh-pages"
check "restarted onto the new areas and rewrote the scrape from raw, adding the area's 2,016 listings" 'echo "$out" | grep -q "restarting" && echo "$out" | grep -q "(10072 listings;" && [ "$(grep -c "^[0-9]" "$W"/data/v3/apts-*.txt)" = 10072 ]'
check "forced a full rebuild and republished, without spending calls" 'echo "$out" | grep -q "rebuilding every month" && [ "$(git -C "$W/origin.git" rev-parse gh-pages)" != "$before" ] && grep -q "\"published\": \"[0-9a-f]\{40\}\"" "$W"/data/runs/2026-11-21.json && [ "$(calls)" = 23 ]'
out=$(run normal 2026-11-24); echo "$out" | grep -E "RESULT|wrote|rebuilding|already"
check "next day: nothing rewritten or rebuilt" '! echo "$out" | grep -q "wrote" && ! echo "$out" | grep -q "rebuilding" && echo "$out" | grep -q "already collected"'

}

g_backup() {
echo "=== backup: backups: one archive per period, retried after failure, full restore ==="
setup
git init -q --bare -b main "$W/backup.git"
git clone -q "$W/backup.git" "$W/backup" 2>/dev/null
git -C "$W/backup" config user.name server; git -C "$W/backup" config user.email server@test
bk() { NYC_RENTMAP_BACKUP_DIR="$W/backup" "$PY" -I "$T/scenario.py" "$W/server" "$W/data" "$1" "$2" 2>&1; }
backups() { git -C "$W/backup.git" rev-list --count main 2>/dev/null || echo 0; }
mv "$W/backup.git" "$W/backup.offline"
out=$(bk normal 2026-11-22); echo "$out" | grep -E "RESULT|backed up"
check "backup remote down: collection and publish still done, run stops on the backup" '[ "$(calls)" = 23 ] && [ "$(origin_q)" = "32 closed 23 23" ] && [ "$(pages_commits)" = 1 ] && echo "$out" | grep -q "RESULT stopped: backup: git fetch"'
check "no backup recorded in the ledger" '! grep -q "\"backed_up\"" "$W"/data/runs/2026-11-21.json'
mv "$W/backup.offline" "$W/backup.git"
out=$(bk normal 2026-11-23); echo "$out" | grep -E "RESULT|backed up|already"
check "next day: period backed up as one archive, recorded in the ledger" 'echo "$out" | grep -q "backed up period 2026-11-21" && [ "$(git -C "$W/backup.git" ls-tree --name-only main)" = "2026-11-21.tar.xz" ] && grep -q "\"backed_up\": \"[0-9a-f]\{40\}\"" "$W"/data/runs/2026-11-21.json'
git -C "$W/backup.git" show main:2026-11-21.tar.xz > "$W/archive.tar.xz"
check "archive holds the ledger and all 23 raw responses" '[ "$(tar -tJf "$W/archive.tar.xz" | grep -c "^raw/2026-11-21/.*\.json$")" = 23 ] && tar -tJf "$W/archive.tar.xz" | grep -qx "runs/2026-11-21.json"'
check "archived ledger leaves out publish and backup fields" '! tar -xOJf "$W/archive.tar.xz" runs/2026-11-21.json | grep -q -E "\"(published|published_at|scrape_filter|backed_up)\""'
out=$(bk normal 2026-11-24); echo "$out" | grep -E "RESULT|backed up"
check "rerun makes no new backup commit" '[ "$(backups)" = 1 ] && ! echo "$out" | grep -q "backed up"'
pages=$(git -C "$W/origin.git" rev-parse gh-pages)
rm -rf "$W/data"; mkdir "$W/data"
out=$(bk mainrestore 2026-11-25); echo "$out" | grep -E "RESULT|restored"
check "restore unpacked the ledger and 23 raw responses" 'echo "$out" | grep -q "restored 24 files (0 already present)" && [ "$(ls "$W"/data/raw/2026-11-21/*.json.gz | wc -l | tr -d " ")" = 23 ]'
out=$(bk mainpublish 2026-11-25); echo "$out" | grep -E "RESULT|wrote|gh-pages"
check "publish after restore rebuilt the scrape and an identical site" 'echo "$out" | grep -q "(8056 listings;" && echo "$out" | grep -q "gh-pages unchanged" && [ "$(git -C "$W/origin.git" rev-parse gh-pages)" = "$pages" ]'

}

g_analytics() {
echo "=== analytics: GoatCounter tag added only when configured ==="
setup
out=$(run normal 2026-11-22); echo "$out" | grep RESULT
TAG='<script data-goatcounter="https://test.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script>'
before=$(git -C "$W/origin.git" rev-parse gh-pages)
before_tree=$(git -C "$W/origin.git" rev-parse "gh-pages^{tree}")
out=$(RENTMAP_GOATCOUNTER_URL=https://test.goatcounter.com/count run publish 2026-11-25); echo "$out" | grep -E "RESULT|gh-pages"
check "setting the count URL republishes without a full rebuild" 'echo "$out" | grep -q "RESULT ok" && [ "$(git -C "$W/origin.git" rev-parse gh-pages)" != "$before" ] && ! echo "$out" | grep -q "rebuilding every month"'
check "both pages carry the tag once, right before </body>" '(for f in index.html trends.html; do [ "$(pages_file $f | grep -cF "$TAG")" = 1 ] && pages_file $f | grep -A1 -F "$TAG" | tail -1 | grep -q "^</body>" || exit 1; done)'
check "pages otherwise unchanged" '(for f in index.html trends.html; do pages_file $f | grep -vF "$TAG" | sed "s/<title>NYC /<title>Boston /" | cmp -s - "$REPO/site/$f" || exit 1; done)'
with=$(git -C "$W/origin.git" rev-parse gh-pages)
out=$(RENTMAP_GOATCOUNTER_URL='https://x.goatcounter.com/count"><script>' run publish 2026-11-25); echo "$out" | grep -E "RESULT"
check "a malformed URL stops the build before publishing" 'echo "$out" | grep -q "RESULT stopped: site build failed: RENTMAP_GOATCOUNTER_URL must be an https URL" && [ "$(git -C "$W/origin.git" rev-parse gh-pages)" = "$with" ]'
out=$(run publish 2026-11-25); echo "$out" | grep -E "RESULT|gh-pages"
check "unsetting it republishes the original files" '[ "$(git -C "$W/origin.git" rev-parse gh-pages)" != "$with" ] && [ "$(git -C "$W/origin.git" rev-parse "gh-pages^{tree}")" = "$before_tree" ] && ! pages_file index.html | grep -q goatcounter'
check "wrapper script parses" 'sh -n "$REPO/nyc/deploy/nyc-rentmap"'

}

ALL_GROUPS="publish quota failures restart alert areas scrape refilter backup analytics"
if [ $# = 0 ]; then set -- $ALL_GROUPS; fi
for g in "$@"; do
  case " $ALL_GROUPS " in
    *" $g "*) ;;
    *) echo "unknown test group: $g (groups: $ALL_GROUPS)" >&2; rm -rf "$W"; exit 2 ;;
  esac
done
for g in "$@"; do "g_$g"; done

echo; echo "$PASS passed, $FAIL failed"
if [ "$FAIL" = 0 ]; then rm -rf "$W"; else echo "test repos kept in $W"; exit 1; fi
