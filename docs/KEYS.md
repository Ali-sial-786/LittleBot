# API keys and tokens

LittleBot uses a few paid services, each with its own key. You can add, change, check and remove every one of
them from your phone: open the dashboard, then **More → Keys** (or **Settings → API keys**). No ssh, no restart.

## The keys

| Key | What LittleBot uses it for | Where to get one |
|-----|----------------------------|------------------|
| Anthropic (Claude) | Claude, the brain: it answers, uses the tools and puts cards up | [console.anthropic.com](https://console.anthropic.com/settings/keys) (a separate, paid API account: a Claude.ai plan doesn't include it) |
| OpenAI | Backup speech-to-text and voice, and GPT when it is chosen instead of Claude | [platform.openai.com](https://platform.openai.com/api-keys) |
| Deepgram | Fast speech-to-text that knows when you have finished talking | [console.deepgram.com](https://console.deepgram.com/) |
| Cartesia | The fastest natural voice | [play.cartesia.ai](https://play.cartesia.ai/keys) |
| ElevenLabs | A natural voice, used when Cartesia isn't set or doesn't answer | [elevenlabs.io](https://elevenlabs.io/app/settings/api-keys) |
| Home Assistant | Your lights, plugs and fans (a long-lived access token; also set `skills.home_assistant_url`) | Your Home Assistant profile page, "Long-lived access tokens" |
| TypeSafe (Jev) | The fast decider (only when it is chosen, docs/DECISIONS.md) | [typesafe.ai](https://typesafe.ai/) |

With Deepgram and Cartesia (or ElevenLabs) set, "Hey LittleBot" uses the fast assistant voice
([ASSISTANT.md](ASSISTANT.md)); without them, GPT-Live as before.

## Saving a key

Paste the key and press **Save**. LittleBot first tries it with one small request that reads nothing of yours (for
example, the list of models):

* the service says no: nothing is saved ("Cartesia refused that key.");
* no answer in 10 seconds, or the service has trouble of its own: the key is saved anyway, and the page says it
  couldn't be checked;
* cloud communication is off (Privacy page) or LittleBot is in offline mode: the key is saved without being tried
  (nothing is sent anywhere). TypeSafe keys can't be tried at all and are saved as they are.

A saved key is used at once: the next question uses it. **Check** tries the key in use again. The page then only ever
shows the last four characters of a key ("ends in …abcd"); the robot never sends a key back to the phone, and its log
only says which key was saved, checked or removed.

## Where keys are kept, and which one wins

LittleBot looks in three places and uses the first that has the key:

1. **Saved on the Keys page**: `data/keys/<name>` on the robot (the folder and the files only LittleBot's user can
   read: chmod 700 / 600).
2. **The `.env` file** (`~/LittleBot/.env`, e.g. `CARTESIA_API_KEY=...`), or the environment. It still works exactly
   as before. LittleBot never writes or changes it.
3. **An older key file**, `data/<name>_api_key`, saved by `scripts/set_key.py` (used only when no other user can read
   it).

So a key saved on the Keys page is used instead of the one in `.env`. **Remove saved key** deletes it, and LittleBot
goes back to the `.env` key (read again at that moment), or the older file, or no key. The badge on each row says
where the key in use comes from: *Saved here*, *From the .env file*, *From an older key file*, or *Not set*.

In offline mode LittleBot uses no keys at all, wherever they are kept.
