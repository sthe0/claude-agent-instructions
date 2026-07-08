---
name: 2026-07-08-secret-on-argv-curl-config-stdin
description: A shell script (ccgram-watchdog.sh) passed a Telegram bot token in the curl URL argument, so osquery captured the full token into Splunk and into a SECALERTS ticket (hunt fired risk 90, curl_exfiltration — a false positive for C2 but a real secret-on-argv leak). Fix: feed the token to curl via a printf-built '-K -' config stream on stdin so nothing sensitive reaches argv (mirroring the already-hardened sibling ccgram-topic-sweep.sh); rotate the compromised token in BotFather (a logged secret is compromised — hardening the emitter does not un-leak the past value); answer SOC and let them close as false-positive.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [SECALERTS-1123779, mirror-working-caller-before-bypass, verify-load-bearing-axis]
created: 2026-07-08
last_verified: 2026-07-08
---

# Secret on curl argv leaks to osquery/Splunk — feed it via a --config stdin stream, and rotate the already-logged value

## Difficulty
A secret passed as a curl command-line argument (token in the URL, or --data with the value) is captured by ps/osquery cmdline and lands verbatim in the endpoint-hunt SIEM (Splunk) and any incident ticket it raises. Static code review does not flag it as a leak; it surfaces only as a downstream security alert. Two follow-on traps: (1) hardening the emitter does NOT neutralise the value already in the log store — a logged live secret must be rotated; (2) the curl config-file double-quoted value parser processes backslash and double-quote escapes, so any free-form field piped into the config stream must be escaped (backslash first, then double-quote) or a stray char desyncs the whole config.

## Order & criterion
1 harden send path (move token off argv into 'printf ... | curl -K -' stdin config, escape free-form fields); 2 prove argv-clean by capturing the live /proc/pid/cmdline during a real send, not by static grep; 3 rotate the compromised token (getMe: new ok:true, old 401) and restart the daemon; 4 answer SOC with root-cause+remediation+rotation, request false-positive closure.

**Acceptance check:** acceptance_review: argv-clean and E2E delivery are measurable (live cmdline capture + message arrival); the false-positive judgement and closure are accepted by SOC on the ticket.

## Contexts

### 2026-07-08 — secret-on-argv → curl -K - stdin config
- Where it arose: the0.klg.yp-c.yandex.net; CCGram watchdog; SECALERTS-1123779
- Working plan: 4-stage TOML plan (harden → argv-clean+E2E verify → rotate token → answer SOC); mirrored the working sibling caller (ccgram-topic-sweep.sh tg_delete) rather than inventing a new mechanism.

## Cost
~1 session; 4 stages; in-thread edits + tracker + BotFather (user-side revoke).

## Self-critique of the agent system
The watchdog was missed when its sibling ccgram-topic-sweep.sh was hardened with the same pattern — a secret-hygiene fix applied to one emitter should trigger a grep of all sibling emitters (curl|wget with an interpolated token) in the same family, not just the one in hand.
