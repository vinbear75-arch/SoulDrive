import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";

const app = readFileSync(resolve("src", "App.tsx"), "utf8");

test("schedule cards expose editing and preserve the configured start time", () => {
  assert.match(app, /className="schedule-edit-action"/);
  assert.match(app, /修改日程/);
  assert.match(app, /formatScheduleStartTime\(artifact\.plan\?\.start_at\)/);
  assert.match(app, /开始时间（创建时设置）/);
  assert.match(app, /method: isScheduleEdit \? "PATCH" : "POST"/);
  assert.match(app, /reminder_sound_enabled: scheduleSoundEnabled/);
  assert.match(app, /aria-pressed=\{scheduleSoundEnabled\}/);
  assert.match(app, /提前 10 分钟声音提醒：/);
});

test("schedule reminder opens a centered in-app card and attempts an unlocked tone", () => {
  assert.match(app, /schedule-reminder-backdrop/);
  assert.match(app, /还有 10 分钟就要开始这一项日程了/);
  assert.match(app, /scheduleReminderAudioContextRef/);
  assert.match(app, /for \(let index = 0; index < 3; index \+= 1\)/);
  assert.match(app, /context\.currentTime \+ index \* 0\.45/);
  assert.match(app, /context\.resume\(\)\.then\(playTone\)/);
  assert.match(app, /nextReminder\.artifact\.plan\?\.reminder_sound_enabled === false\) return/);
  assert.match(app, /Math\.ceil\(\(startMs - scheduleNowMs\) \/ \(60 \* 1000\)\)/);
  assert.match(app, /scheduleReminderLiveMessage/);
  assert.match(app, /小于 1 分钟后开始/);
});

test("started schedules leave the top rail and open a one-time started notice", () => {
  assert.match(app, /const \[scheduleNowMs, setScheduleNowMs\] = useState\(\(\) => Date\.now\(\)\)/);
  assert.match(app, /startMs > scheduleNowMs/);
  assert.match(app, /scheduleStartedEligibleRef/);
  assert.match(app, /scheduleStartedNotifiedRef/);
  assert.match(app, /日程已开始/);
  assert.match(app, /scheduleReminderPhase === "started"/);
});
