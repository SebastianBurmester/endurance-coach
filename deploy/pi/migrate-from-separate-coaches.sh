#!/bin/sh
# One-time switch of the Pi from the separate Garmin coach (~/garmin_ai) and COROS coach (~/coros_ai) to this repo.
# Both checkouts keep their folder (= systemd instance name), data, profile and plan. Run on the Pi as pi:
#   sh migrate-from-separate-coaches.sh /path/to/endurance.bundle   (or without an argument once the deploy key works)
set -eu
bundle=${1:-}
units=~/.config/systemd/user

echo "== stop the old units (wait for running syncs / updates)"
systemctl --user stop garmin-coach-update.timer coros-coach-update.timer garmin-coach-sync.timer coros-coach-sync.timer
while pgrep -f "[g]armin-coach sync|[c]oros-coach sync|deploy/pi/update.sh" >/dev/null; do sleep 2; done
systemctl --user stop garmin-coach-web.service coros-coach-web.service garmin-coach-remote.service
systemctl --user disable -q garmin-coach-update.timer coros-coach-update.timer garmin-coach-sync.timer \
  coros-coach-sync.timer garmin-coach-web.service coros-coach-web.service garmin-coach-remote.service \
  coros-coach-remote.service 2>/dev/null || true

stamp=$(date +%Y%m%d-%H%M)
for d in garmin_ai coros_ai; do
  echo "== $d: back up data and profile to ~/backup-$d-$stamp"
  mkdir -p ~/backup-$d-$stamp
  cp -a ~/$d/data ~/backup-$d-$stamp/
  [ -f ~/$d/athlete.json ] && cp -a ~/$d/athlete.json ~/backup-$d-$stamp/
  echo "== $d: switch to the endurance-coach repo"
  cd ~/$d
  git remote set-url origin git@github-endurance-coach:SebastianBurmester/endurance-coach.git
  if [ -n "$bundle" ]; then
    git fetch -q "$bundle" main:refs/remotes/origin/main
  else
    git fetch -q origin main
  fi
  git checkout -q -B main origin/main
  git reset -q --hard origin/main
  git branch -q --set-upstream-to=origin/main main
  .venv/bin/pip uninstall -q -y garmin-coach coros-coach 2>/dev/null || true
  .venv/bin/pip install -q -e .
  mkdir -p data/auth && chmod 700 data/auth
done

echo "== move each login into its athlete's checkout (no shared keys in ~/.config any more)"
if [ -f ~/.config/garmin/connect-personal-api-key ]; then
  mv ~/.config/garmin/connect-personal-api-key ~/garmin_ai/data/auth/garmin-api-key
  rm -f ~/.config/garmin/connect-personal-api-bearer-token.json
fi
if [ -f ~/.config/coros-coach/auth.json ]; then
  mv ~/.config/coros-coach/auth.json ~/coros_ai/data/auth/coros.json
fi
chmod 600 ~/garmin_ai/data/auth/* ~/coros_ai/data/auth/* 2>/dev/null || true

echo "== per-checkout settings"
cat > ~/garmin_ai/coach.env <<'ENV'
COACH_NAME="Garmin Coach"
COACH_PREFIX=garmin-coach
COACH_PORT=8090
ENV
cat > ~/coros_ai/coach.env <<'ENV'
COACH_NAME="Tri Coach"
COACH_PREFIX=tri-coach
COACH_PORT=8091
CLAUDE_CONFIG_DIR=/home/pi/.claude-coros
ENV
(cd ~/garmin_ai && .venv/bin/coach setup --source garmin --focus road_cycling >/dev/null)
(cd ~/coros_ai && .venv/bin/coach setup --source coros --focus triathlon >/dev/null)

echo "== swap the systemd units"
rm -f $units/garmin-coach-*.service $units/garmin-coach-*.timer $units/coros-coach-*.service $units/coros-coach-*.timer
cp ~/garmin_ai/deploy/pi/coach-*@.* $units/
systemctl --user daemon-reload
for d in garmin_ai coros_ai; do
  systemctl --user enable -q --now "coach-web@$d.service" "coach-sync@$d.timer" "coach-update@$d.timer"
done
systemctl --user enable -q --now coach-remote@garmin_ai.service
# coach-remote@coros_ai waits for her own Claude login (see README).

echo "== first sync (Garmin: adopts the old cache and re-reads all FIT files once, ~10-20 min on the Pi)"
systemctl --user start --no-block coach-sync@coros_ai.service coach-sync@garmin_ai.service
systemctl --user list-units "coach-*" --all --no-pager --plain | grep -v "^$"
