"""Instant intents: whole plain utterances only; everything else goes to the brain."""

from __future__ import annotations

import pytest

from littlebot.assistant import intents
from littlebot.chatgpt.tools import RobotToolbox
from littlebot.skills.base import Skill
from littlebot.testing import make_test_context


@pytest.mark.parametrize("text,name", [
    ("Stop.", "stop"), ("never mind", "stop"), ("okay stop please", "stop"), ("Thanks!", "thanks"),
    ("thank you so much", "thanks"), ("bye", "bye"), ("that's all for now", "stop"), ("louder", "volume"),
    ("turn it down please", "volume"), ("volume 40", "volume"), ("set the volume to 70 percent", "volume"),
    ("what time is it?", "time"), ("What's the time", "time"), ("end the call", "stop"), ("hang up", "stop"),
    ("stop listening", "stop"), ("okay end the conversation", "stop"), ("you can go now", "stop"),
    # told to stop in whatever words: the call ends without a word (the robot, 2026-10-31)
    ("Hey LittleBot, stop talking", "stop"), ("stop, stop", "stop"), ("no stop", "stop"), ("I said stop", "stop"),
    ("can you stop talking", "stop"), ("could you please be quiet", "stop"), ("stop stop stop", "stop"),
    ("alright that's enough", "stop"), ("good night", "bye"), ("see you later", "bye"),
    # speech-to-text often ends a sentence after the first word (the robot, 2026-10-31)
    ("Thanks. Bye.", "bye"), ("Thanks! Bye!", "bye"), ("Okay. Stop talking.", "stop"), ("Okay. Thanks.", "thanks"),
    ("Hey. Little bot. Stop.", "stop"),
    # "stop talking" with its "stop" lost under the voice's last words (the robot, 2026-10-31)
    ("Talking.", "stop"), ("talking!", "stop"), ("No talking.", "stop"),
])
def test_matches(text: str, name: str) -> None:
    hit = intents.match(text)
    assert hit is not None and hit.name == name


@pytest.mark.parametrize("text", [
    "stop the war in my head and tell me a story", "what time is it in Tokyo", "thanks, and what's the weather",
    "can you play some jazz", "pause", "skip", "how loud can you go", "",
])
def test_not_instant(text: str) -> None:
    assert intents.match(text) is None


@pytest.mark.parametrize("text", ["stop talking", "end the call", "that's all", "you can stop now", "hang up",
                                  # the robot, 2026-11-03: "stop talking means stop listening as well"
                                  "Can you stop talking to him?", "stop talking to her", "Hello, stop talking.",
                                  "I told you to stop talking.", "Stop talking and stop listening.",
                                  "No, I said stop talking.", "Wait. Stop talking to the kids."])
def test_told_to_stop_ends_without_a_word(text: str) -> None:
    hit = intents.match(text)
    assert hit is not None and hit.end and hit.say == ""


@pytest.mark.parametrize("text", ["no", "hey", "stop the music and play jazz", "no, I meant tomorrow",
                                  "can you stop by the store", "what stops the rain", "talking about dinosaurs",
                                  "who was talking", "stop talking about dinosaurs", "wait", "hello",
                                  "can you stop talking to him about the game", "stop talking to him and tell me a joke"])
def test_not_a_stop(text: str) -> None:
    hit = intents.match(text)
    assert hit is None or hit.name != "stop"


def test_media_words_only_with_media() -> None:
    assert intents.match("pause") is None
    assert intents.match("pause", media_on=True).name == "pause"
    assert intents.match("next song", media_on=True).name == "skip"
    assert intents.match("stop the music", media_on=True).name == "stop_media"
    assert intents.match("keep playing", media_on=True).name == "resume"


async def test_run_volume_and_time() -> None:
    ctx = make_test_context()
    box = RobotToolbox(ctx, mode="chat")
    said = await intents.run(intents.match("volume 30"), box, ctx)
    assert said == "Volume 30."
    assert "set_volume" in box.calls
    said = await intents.run(intents.match("what time is it"), box, ctx)
    assert said.startswith("It's ") and ("a.m." in said or "p.m." in said)


@pytest.mark.parametrize("text,step", [
    # these went to the brain, which looked the volume up first: 2-2.3 s to the first word (the robot, 2026-10-31)
    ("Turn the volume down a bit.", -1), ("A little louder, please.", 1), ("turn it up a little", 1),
    ("can you turn the volume down a little bit", -1), ("Make it a bit quieter.", -1), ("It's too loud.", -1),
    ("lower the volume", -1), ("Turn up the volume please.", 1), ("a bit louder", 1), ("Louder, please.", 1),
    ("could you speak up", 1), ("a little bit softer", -1), ("turn it down", -1),
    ("Turned the volume down a bit.", -1),  # how speech-to-text wrote "turn the volume down a bit" (the robot)
])
def test_volume_in_everyday_words(text: str, step: int) -> None:
    hit = intents.match(text)
    assert hit is not None and hit.name == "volume" and hit.args == {"step": step}


@pytest.mark.parametrize("text,percent", [
    ("set the volume to one hundred percent", 100), ("Volume to fifty.", 50), ("Set the volume to seventy-five "
     "percent.", 75), ("volume 40", 40), ("set the volume to max", 100), ("turn the volume to 30", 30),
    ("volume to twenty five", 25), ("volume seven", 7), ("can you set the volume to sixty percent", 60),
])
def test_volume_to_a_number_in_words_or_digits(text: str, percent: int) -> None:
    hit = intents.match(text)
    assert hit is not None and hit.name == "volume" and hit.args == {"percent": percent}


@pytest.mark.parametrize("text", ["a little leopard, please", "turn up the heat", "turn the lights down a bit",
                                  "is it too loud?", "volume of a sphere", "a little later please",
                                  "how loud is a jet engine", "turn it up to eleven, like in the film"])
def test_not_the_volume(text: str) -> None:
    hit = intents.match(text)
    assert hit is None or hit.name != "volume"


async def test_at_the_ceiling_it_says_so_and_never_a_level_it_did_not_set() -> None:
    ctx = make_test_context()  # audio.speaker.max_volume 0.8
    box = RobotToolbox(ctx, mode="chat")
    assert await intents.run(intents.match("set the volume to one hundred percent"), box, ctx) == \
        "Volume 80, as loud as I go."
    assert await intents.run(intents.match("a little louder please"), box, ctx) == "That's as loud as I go."
    await box.run("set_volume", {"percent": 75})
    assert await intents.run(intents.match("louder"), box, ctx) == "Louder. That's as loud as I go."
    assert ctx.audio.volume == pytest.approx(0.8)
    assert await intents.run(intents.match("turn the volume down a bit"), box, ctx) == "Quieter."
    assert ctx.audio.volume == pytest.approx(0.7)
    ctx.audio.set_volume = lambda v: min(float(v), 0.6)  # the speaker itself stops lower than the config's ceiling
    assert await intents.run(intents.match("volume 100"), box, ctx) == "Volume 60, as loud as I go."


async def test_voice_can_never_make_it_silent() -> None:
    ctx = make_test_context()
    box = RobotToolbox(ctx, mode="chat")
    assert await intents.run(intents.match("volume 0"), box, ctx) == "Volume 15, as quiet as I go."
    assert ctx.audio.volume == 0.15  # the quietest it goes by voice
    assert await intents.run(intents.match("softer"), box, ctx) == "That's as quiet as I go."
    assert ctx.audio.volume == 0.15


# ------------------------------------------------------------------ photos and videos (littlebot/skills/camera.py)
@pytest.mark.parametrize("text,name,kind", [
    ("Take a picture.", "photo", None), ("take a photo of us", "photo", None), ("Can you take a selfie?", "photo", None),
    ("Hey LittleBot, take a pic", "photo", None), ("snap a photo of the baby please", "photo", None),
    ("take my picture", "photo", None), ("picture time!", "photo", None), ("take another photo", "photo", None),
    ("Record this.", "record", None), ("start recording", "record", None), ("record a video", "record", None),
    ("make a video of the baby", "record", None), ("can you film us", "record", None), ("take a video", "record", None),
    ("stop recording", "stop_record", None), ("Stop the recording, please.", "stop_record", None),
    ("end the recording", "stop_record", None),
    ("show me my photos", "show_photos", "photo"), ("show the videos", "show_photos", "video"),
    ("open the gallery", "show_photos", None), ("can I see the pictures", "show_photos", "photo"),
    ("show me the last video", "show_photos", "video"),
    ("send it to my phone", "send_photo", None), ("send that photo to my phone", "send_photo", "photo"),
    ("airdrop it to my iPhone", "send_photo", None), ("send me the video", "send_photo", "video"),
    ("share the picture", "send_photo", "photo"),
])
def test_camera_phrases(text: str, name: str, kind: str | None) -> None:
    hit = intents.match(text)
    assert hit is not None and hit.name == name
    if name in ("show_photos", "send_photo"):
        assert hit.args["kind"] == kind


@pytest.mark.parametrize("text,to", [
    ("send it to my phone", "my"), ("send it to my wife's phone", "my wife's"), ("Send that to Ali's phone.", "Ali's"),
    ("send the photo to my husband", "my husband"), ("share that picture to Sara", "Sara"),
    ("send it to my spouse's phone", "my spouse's"), ("send the video to everyone", "everyone"),
    ("put it on our phones", "our"), ("send it to dad's phone", "dad's"), ("send me the photo", None),
    ("send the picture to me", "me"),
])
def test_whose_phone_is_kept(text: str, to: str | None) -> None:
    hit = intents.match(text)
    assert hit is not None and hit.name == "send_photo" and hit.args["to"] == to


@pytest.mark.parametrize("text", [
    "cheese", "say cheese", "smile", "what's in this picture", "take a picture of a cat and describe it",
    "show me a picture of a dog", "send a message to dad", "picture", "record player", "is it recording?",
    "how do I take a good photo", "send it", "show it", "video", "recording",
])
def test_not_a_camera_phrase(text: str) -> None:
    hit = intents.match(text)
    assert hit is None or hit.name not in ("photo", "record", "stop_record", "show_photos", "send_photo")


@pytest.mark.parametrize("text", ["stop", "Stop!", "stop it", "cut", "that's a wrap", "that's it", "we're done",
                                  "stop the video", "done"])
def test_while_recording_stop_means_the_recording(text: str) -> None:
    hit = intents.match(text, recording_on=True)
    assert hit is not None and hit.name == "stop_record" and hit.end
    other = intents.match(text)
    assert other is None or other.name != "stop_record"


def test_while_recording_never_mind_still_ends_the_chat() -> None:
    assert intents.match("never mind", recording_on=True).name == "stop"
    assert intents.match("cancel", recording_on=True).name == "stop"
    # a YouTube video on too: "stop" is about what's playing (a video starting stops a recording anyway)
    assert intents.match("stop the video", recording_on=True, media_on=True).name == "stop_media"


class _Cam(Skill):
    name = "camera"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.asked: list[tuple[str, object]] = []

    async def instant(self, what: str, *, kind: str | None = None, to: str | None = None) -> str:
        from littlebot.skills import takeover

        self.asked.append((what, kind) if to is None else (what, kind, to))
        if what == "photo":
            async def snap() -> bool:
                return True

            takeover.offer(self.ctx, "photo 1", snap)
            return "Okay, smile!"
        return "Here they are."


async def test_a_photo_phrase_queues_the_photo_for_after_the_words() -> None:
    from littlebot.skills.hub import SkillHub

    ctx = make_test_context()
    cam = _Cam(ctx)
    ctx.skills = SkillHub(ctx, [cam])
    box = RobotToolbox(ctx, mode="chat")
    said = await intents.run(intents.match("take a picture"), box, ctx)
    assert said == "Okay, smile!" and [p.label for p in box.pending] == ["photo 1"] and box.pending[0].takeover
    said = await intents.run(intents.match("show me my videos"), box, ctx)
    assert said == "Here they are." and cam.asked[-1] == ("show", "video")
    await intents.run(intents.match("send it to my wife's phone"), box, ctx)
    assert cam.asked[-1] == ("send", None, "my wife's")
    ctx.skills = SkillHub(ctx, [])
    assert await intents.run(intents.match("take a picture"), box, ctx) == "I can't take photos or videos."
    ctx.db.close()

# ---------------------------------------------------------------- always recording (littlebot/capture/buffer.py)
@pytest.mark.parametrize("text,seconds", [
    ("save that", None), ("Save that.", None), ("Hey LittleBot, save that!", None), ("clip that", None),
    ("save that clip", None), ("can you save that please", None), ("save the last few minutes", None),
    ("save the last 3 minutes", 180.0), ("Save the last three minutes.", 180.0), ("save the last minute", 60.0),
    ("save the last two minutes", 120.0), ("save the last 30 seconds", 30.0), ("clip the last minute", 60.0),
    ("save the last 3 minutes of video", 180.0),
])
def test_save_that(text: str, seconds: float | None) -> None:
    hit = intents.match(text)
    assert hit is not None and hit.name == "save_clip" and hit.args["seconds"] == seconds and not hit.end


@pytest.mark.parametrize("text,on", [
    ("turn on always recording", True), ("Turn off always recording.", False), ("turn always recording off", False),
    ("switch always recording on", True), ("always recording on", True), ("stop always recording", False),
    ("please turn on always-recording", True), ("can you turn off the always recording", False),
])
def test_always_recording_on_and_off(text: str, on: bool) -> None:
    hit = intents.match(text)
    assert hit is not None and hit.name == "always_recording" and hit.args["on"] is on


@pytest.mark.parametrize("text", [
    "did you save that?", "what does save that mean", "is always recording on?", "are you always recording?",
    "why are you always recording", "save that song to my playlist", "save the date", "save", "keep that in mind",
    "can you save that for later in my notes", "how do I save the last minute?", "what did you save",
    "save the last dance for me", "always recording", "do you save the last 3 minutes?",
])
def test_not_about_always_recording(text: str) -> None:
    hit = intents.match(text)
    assert hit is None or hit.name not in ("save_clip", "always_recording")


async def test_save_that_goes_to_the_camera_skill_with_how_much() -> None:
    from littlebot.skills.hub import SkillHub

    class Cam(Skill):
        name = "camera"
        asked: list[tuple[str, float | None]] = []

        async def instant(self, what: str, *, kind: str | None = None, to: str | None = None,
                          seconds: float | None = None) -> str:
            self.asked.append((what, seconds))
            return {"save_clip": "Saved the last minute.", "buffer_on": "Always recording is on."}.get(what, "Off.")

    ctx = make_test_context()
    cam = Cam(ctx)
    ctx.skills = SkillHub(ctx, [cam])
    box = RobotToolbox(ctx, mode="chat")
    assert await intents.run(intents.match("save the last minute"), box, ctx) == "Saved the last minute."
    assert await intents.run(intents.match("save that"), box, ctx) == "Saved the last minute."
    assert await intents.run(intents.match("turn on always recording"), box, ctx) == "Always recording is on."
    assert await intents.run(intents.match("turn off always recording"), box, ctx) == "Off."
    assert cam.asked == [("save_clip", 60.0), ("save_clip", None), ("buffer_on", None), ("buffer_off", None)]
    assert box.pending == []  # saved at once: nothing waits for the words to end
    ctx.db.close()
