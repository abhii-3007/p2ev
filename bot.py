import asyncio
import random
import re
import sys
import time
import discord

from config import DISCORD_TOKEN, POKETWO_BOT_ID, HELPER_BOT_IDS
from services.parser import extract_pokemon_name
from services.queue import CatchQueue
from services.stealth import simulate_typo

# discord.py-self does not require intents for user accounts
bot = discord.Client()
catch_queue = CatchQueue()

# --- Helper Bot & Channel Configurations ---
INCENSE_HELPER_BOT_ID = 1307910235737948252
HEADER_HELPER_BOT_ID = 874910942490677270
INCENSE_CHANNELS = {str(i) for i in range(1, 11)}

# Despawn safety window (20s total per channel; skip only if older than 19.3s)
INCENSE_DESPAWN_LIMIT = 19.3

# --- Global State & Locks ---
is_paused = False
is_afk = False
is_incense_mode = False
active_incense_guild: discord.Guild | None = None

# Task tracker for ongoing channel 1-10 command loops (buy / p / r)
incense_cmd_task: asyncio.Task | None = None

# Ensures the bot can NEVER send two messages at the exact same time across any channel
bot_action_lock = asyncio.Lock()

# Tracks the monotonic timestamp of ANY message sent by your account (bot or manual)
last_send_time: float = 0.0

# --- Staggered Incense EDF Scheduler State ---
# Holds latest active spawn per channel: channel_name -> (spawn_timestamp, channel_obj, pokemon_name)
active_channel_spawns: dict[str, tuple[float, discord.abc.Messageable, str]] = {}
incense_wake_event = asyncio.Event()
incense_worker_task: asyncio.Task | None = None

# Tracks how many pokemon are currently queued or being caught in Normal Mode
pending_catches = 0

# --- Smart Miss Counter Variables ---
ping_counter = 0
next_miss_target = random.randint(10, 15)


def get_pokemon_name(content: str) -> str | None:
    """
    Extracts the Pokemon name from the '## Swalot <:_:...>【<:...>】' format first,
    then falls back to services.parser.extract_pokemon_name.
    """
    header_match = re.search(r"^##\s+([^<【\n]+)", content.strip(), re.MULTILINE)
    if header_match:
        return header_match.group(1).strip()
    return extract_pokemon_name(content)


async def paced_send(
    channel: discord.abc.Messageable,
    content: str,
    min_gap: float = 1.35,
    max_gap: float = 1.65,
) -> float:
    """
    Acquires bot_action_lock and sends a message while respecting a 1.35s-1.65s
    cooldown from the last completed send (or manual user message).
    Prevents Discord 429 rate limits while catching 10 spawns in ~15s (< 20s window).
    """
    global last_send_time
    async with bot_action_lock:
        target_gap = random.uniform(min_gap, max_gap)
        elapsed_since_last = time.monotonic() - last_send_time
        remaining_sleep = max(0.0, target_gap - elapsed_since_last)

        if remaining_sleep > 0:
            await asyncio.sleep(remaining_sleep)

        await channel.send(content)
        last_send_time = time.monotonic()
        return target_gap


async def incense_scheduler_loop() -> None:
    """
    Earliest-Deadline-First (EDF) worker for Incense Mode.
    Always catches the oldest waiting channel first (Channel 1 -> 2 -> 3 ... -> 10).
    """
    while True:
        await incense_wake_event.wait()

        while active_channel_spawns and is_incense_mode and not is_paused:
            # Pick the channel whose spawn appeared earliest (Earliest Deadline First)
            oldest_channel = min(
                active_channel_spawns.keys(),
                key=lambda ch: active_channel_spawns[ch][0],
            )
            spawn_ts, channel_obj, pokemon_name = active_channel_spawns.pop(oldest_channel)

            age = time.monotonic() - spawn_ts
            if age >= INCENSE_DESPAWN_LIMIT:
                print(
                    f"⏳ [Incense] Dropped expired {pokemon_name} in #{oldest_channel} "
                    f"(Age: {age:.1f}s >= 20s)"
                )
                continue

            final_name = pokemon_name.lower() if random.random() < 0.70 else pokemon_name
            cmd = random.choice(["c", "catch"])
            final_message = f"<@{POKETWO_BOT_ID}> {cmd} {final_name}"

            try:
                # If backlog is high (>= 5 waiting), slightly tighten gap to 1.25s-1.45s
                backlog = len(active_channel_spawns)
                min_g, max_g = (1.25, 1.45) if backlog >= 5 else (1.35, 1.65)

                gap_used = await paced_send(
                    channel_obj, final_message, min_gap=min_g, max_gap=max_g
                )
                total_age = time.monotonic() - spawn_ts
                print(
                    f"🏓 [Incense] Caught {final_name} in #{oldest_channel} "
                    f"(Pace: {int(gap_used * 1000)}ms | Spawn Age: {total_age:.2f}s/20s | "
                    f"Remaining in wave: {len(active_channel_spawns)})"
                )
            except Exception as e:
                print(f"⚠️ [Incense] Send error in #{oldest_channel}: {e}")

        incense_wake_event.clear()


async def afk_timer(afk_seconds: int) -> None:
    global is_afk
    minutes = afk_seconds // 60
    seconds = afk_seconds % 60
    print(f"🚶 [Stealth] Taking a bathroom break. AFK for {minutes} minutes and {seconds} seconds.")

    await asyncio.sleep(afk_seconds)

    is_afk = False
    print("🔙 [Stealth] Back at the keyboard. Resuming auto-catch automatically!")


async def run_incense_channel_command(
    guild: discord.Guild,
    subcommand: str,
    action_label: str,
    abort_if_paused: bool = True,
) -> None:
    """
    Sends '<@POKETWO_BOT_ID> <subcommand>' across channels 1 to 10 sequentially.
    """
    print(f"🔥 [Incense] Starting '{subcommand}' ({action_label}) across channels 1 to 10...")
    for i in range(1, 11):
        if abort_if_paused and (is_paused or not is_incense_mode):
            print(f"⏹️ [Incense] '{subcommand}' sequence halted (bot paused or switched to normal mode).")
            return

        channel = discord.utils.get(guild.text_channels, name=str(i))
        if channel is None:
            print(f"⚠️ [Incense] Channel '{i}' not found in this server. Skipping.")
            continue

        # Yield briefly so any ready EDF spawn catch can interleave smoothly
        await asyncio.sleep(random.uniform(1.35, 1.65))

        if abort_if_paused and (is_paused or not is_incense_mode):
            print(f"⏹️ [Incense] '{subcommand}' sequence halted before sending.")
            return

        cmd_msg = f"<@{POKETWO_BOT_ID}> {subcommand}"
        try:
            gap_used = await paced_send(channel, cmd_msg, min_gap=1.35, max_gap=1.65)
            print(f"✅ [Incense] Sent '{subcommand}' in #{channel.name} (Pace: {int(gap_used * 1000)}ms)")
        except Exception as e:
            print(f"⚠️ [Incense] Failed sending '{subcommand}' in #{channel.name}: {e}")

    print(f"🎉 [Incense] Finished '{subcommand}' ({action_label}) in channels 1 to 10!")


def start_incense_channel_task(
    guild: discord.Guild,
    subcommand: str,
    action_label: str,
    abort_if_paused: bool = True,
) -> None:
    """Cancels any previous 1-10 channel command loop before starting the new one."""
    global incense_cmd_task
    if incense_cmd_task and not incense_cmd_task.done():
        incense_cmd_task.cancel()
    incense_cmd_task = bot.loop.create_task(
        run_incense_channel_command(guild, subcommand, action_label, abort_if_paused)
    )


@bot.event
async def on_ready() -> None:
    global incense_worker_task
    if incense_worker_task is None or incense_worker_task.done():
        incense_worker_task = bot.loop.create_task(incense_scheduler_loop())

    print(f"Logged in as {bot.user} | Staggered EDF Incense Catcher + 429 Guard Active.")
    print(f"🎯 [Stealth] Next forced miss is scheduled in {next_miss_target} pings.")


@bot.event
async def on_message(message: discord.Message) -> None:
    global is_paused, is_afk, is_incense_mode, active_incense_guild
    global ping_counter, next_miss_target, pending_catches, last_send_time

    msg_content = message.content.strip().lower()

    # 1. Handle own messages & commands
    if message.author.id == bot.user.id:
        # Record timestamp of ANY message sent by your account (including manual commands)
        # so the incense scheduler automatically spaces its next send to avoid 429 rate limits
        last_send_time = time.monotonic()

        if msg_content == "!pause":
            is_paused = True
            active_channel_spawns.clear()
            print("⏸️ Bot manually paused.")
            return

        if msg_content == "!resume":
            is_paused = False
            if is_incense_mode and active_channel_spawns:
                incense_wake_event.set()
            mode_str = "INCENSE MODE" if is_incense_mode else "NORMAL MODE"
            print(f"▶️ Bot manually resumed in {mode_str}.")
            return

        if msg_content == "!inc_start":
            is_incense_mode = True
            is_paused = False
            is_afk = False
            active_channel_spawns.clear()
            if message.guild:
                active_incense_guild = message.guild
                print("🔥 [Mode Switch] INCENSE MODE STARTED. Staggering buy across 1-10 & catching immediately.")
                start_incense_channel_task(
                    message.guild, "inc buy -y", "Buy Incense", abort_if_paused=True
                )
            return

        if msg_content == "!inc_pause":
            is_paused = True
            active_channel_spawns.clear()
            target_guild = message.guild or active_incense_guild
            print("⏸️ [Incense] !inc_pause triggered. Clearing queue and sending 'inc p' to channels 1-10...")
            if target_guild:
                start_incense_channel_task(
                    target_guild, "inc p", "Pause Incense", abort_if_paused=False
                )
            return

        if msg_content == "!inc_resume":
            is_incense_mode = True
            is_paused = False
            is_afk = False
            active_channel_spawns.clear()
            target_guild = message.guild or active_incense_guild
            print("▶️ [Incense] !inc_resume triggered. Resuming catches and sending 'inc r' to channels 1-10...")
            if target_guild:
                active_incense_guild = target_guild
                start_incense_channel_task(
                    target_guild, "inc r", "Resume Incense", abort_if_paused=True
                )
            return

        if msg_content == "!normal":
            is_incense_mode = False
            active_channel_spawns.clear()
            if incense_cmd_task and not incense_cmd_task.done():
                incense_cmd_task.cancel()
            print("🌿 [Mode Switch] NORMAL MODE ACTIVATED. Incense mode is now OFF.")
            return

        return

    # 2. Safety conditions (Captcha)
    if (
        message.author.id == POKETWO_BOT_ID
        and "Please tell us you're human!" in message.content
        and str(bot.user.id) in message.content
    ):
        is_paused = True
        active_channel_spawns.clear()
        print("⚠️ Captcha detected targeting YOUR account. Script paused immediately!")

        if is_incense_mode:
            target_guild = message.guild or active_incense_guild
            if target_guild:
                print("🛑 [Incense] Captcha detected during Incense Mode! Sending 'inc p' across channels 1 to 10...")
                start_incense_channel_task(
                    target_guild, "inc p", "Captcha Auto-Pause Incense", abort_if_paused=False
                )
        return

    # 3. Shiny Catch Sequence
    if (
        message.author.id == POKETWO_BOT_ID
        and "These colors seem unusual..." in message.content
        and str(bot.user.id) in message.content
    ):
        if is_incense_mode:
            print(f"✨ [Incense] Shiny caught in #{getattr(message.channel, 'name', 'unknown')}! Continuing wave without pausing...")
            return

        is_paused = True
        print("✨ Shiny caught - script paused. Initiating reaction sequence.")

        async def shiny_reaction_sequence():
            async with bot_action_lock:
                reaction = random.choice(["YOOOOOO", "finallyyy", "damnnn", "yayyaya"])
                await asyncio.sleep(random.uniform(1.6, 3.0))
                async with message.channel.typing():
                    await asyncio.sleep(len(reaction) * random.uniform(0.08, 0.16))
                await message.channel.send(reaction)

                ping_msg = f"<@{POKETWO_BOT_ID}> i l"
                await asyncio.sleep(random.uniform(2.4, 5.0))
                async with message.channel.typing():
                    await asyncio.sleep(len(ping_msg) * random.uniform(0.08, 0.16))
                await message.channel.send(ping_msg)

                final_msg = "tyty!"
                await asyncio.sleep(random.uniform(2.0, 4.0))
                async with message.channel.typing():
                    await asyncio.sleep(len(final_msg) * random.uniform(0.08, 0.16))
                await message.channel.send(final_msg)

        bot.loop.create_task(shiny_reaction_sequence())
        return

    if is_paused or (is_afk and not is_incense_mode):
        return

    # =========================================================================
    # 4A. INCENSE MODE CATCHING LOGIC (Staggered EDF Scheduler)
    # =========================================================================
    if is_incense_mode:
        channel_name = getattr(message.channel, "name", "")
        if message.author.id == INCENSE_HELPER_BOT_ID and channel_name in INCENSE_CHANNELS:
            if message.guild:
                active_incense_guild = message.guild

            pokemon_name = get_pokemon_name(message.content)
            if not pokemon_name:
                return

            now = time.monotonic()
            if channel_name in active_channel_spawns:
                old_ts, _, old_poke = active_channel_spawns[channel_name]
                print(
                    f"🔄 [Incense] #{channel_name} replaced '{old_poke}' ({now - old_ts:.1f}s old) "
                    f"with new spawn '{pokemon_name}'"
                )

            # Register spawn in the EDF map and wake the scheduler immediately
            active_channel_spawns[channel_name] = (now, message.channel, pokemon_name)
            incense_wake_event.set()
        return

    # =========================================================================
    # 4B. NORMAL MODE CATCHING LOGIC (Uses CatchQueue + Full Stealth Delays)
    # =========================================================================
    is_pinged_helper = (
        message.author.id in HELPER_BOT_IDS and str(bot.user.id) in message.content
    )
    is_header_helper = (
        message.author.id == HEADER_HELPER_BOT_ID and message.content.strip().startswith("## ")
    )

    if is_pinged_helper or is_header_helper:

        # --- Smart Miss Logic ---
        ping_counter += 1
        if ping_counter >= next_miss_target:
            print(f"🙈 [Stealth] Simulated human error: Ignored ping #{ping_counter}.")
            ping_counter = 0
            next_miss_target = random.randint(10, 15)
            print(f"🎯 [Stealth] Next forced miss is scheduled in {next_miss_target} pings.")
            return

        # --- AFK Logic ---
        if random.random() < 0.002:
            is_afk = True
            afk_seconds = random.randint(180, 300)
            bot.loop.create_task(afk_timer(afk_seconds))
            return

        pokemon_name = get_pokemon_name(message.content)
        if not pokemon_name:
            return

        pending_catches += 1
        print(f"\n📥 Ping {ping_counter}/{next_miss_target} added to the queue: {pokemon_name} (Total Pending: {pending_catches})")

        async def process_notification() -> None:
            global pending_catches
            try:
                if is_paused or is_afk or is_incense_mode:
                    return

                print(f"⚙️ Processing queued catch for: {pokemon_name}")

                read_delay = random.uniform(0.4, 0.8)
                if random.random() < 0.10:
                    distraction_time = random.uniform(2.0, 5.0)
                    read_delay += distraction_time
                    print(f"[Stealth] Distraction triggered. Delaying reaction by {int(distraction_time * 1000)}ms")

                await asyncio.sleep(read_delay)

                async with bot_action_lock:
                    if is_paused or is_afk or is_incense_mode:
                        return

                    ms_per_char = random.uniform(0.04, 0.08)
                    typing_delay = len(pokemon_name) * ms_per_char

                    async with message.channel.typing():
                        await asyncio.sleep(typing_delay)

                    final_name = pokemon_name
                    made_typo = False
                    if random.random() < 0.05:
                        final_name = simulate_typo(final_name)
                        made_typo = True
                        print(f"[Stealth] Made a typo: {final_name}")

                    cmd = random.choice(["c", "catch"])
                    if random.random() < 0.70:
                        final_name = final_name.lower()

                    r_space = random.random()
                    extra_space = "" if r_space < 0.03 else ("  " if r_space < 0.3 else " ")
                    final_message = f"<@{POKETWO_BOT_ID}>{extra_space}{cmd} {final_name}"

                    await message.channel.send(final_message)
                    print(f"🏓 Caught: {final_name} (Read: {int(read_delay * 1000)}ms | Typed: {int(typing_delay * 1000)}ms)")

                if made_typo:
                    realize_delay = random.uniform(0.5, 1.5)
                    await asyncio.sleep(realize_delay)

                    async with bot_action_lock:
                        if is_paused or is_afk or is_incense_mode:
                            return

                        correct_typing_delay = len(pokemon_name) * random.uniform(0.04, 0.08)
                        async with message.channel.typing():
                            await asyncio.sleep(correct_typing_delay)

                        correct_cmd = random.choice(["c", "catch"])
                        correct_name = pokemon_name.lower() if random.random() < 0.70 else pokemon_name
                        correct_msg = f"<@{POKETWO_BOT_ID}> {correct_cmd} {correct_name}"

                        await message.channel.send(correct_msg)
                        print(f"🔧 [Stealth] Corrected typo quickly with: {correct_name}")

                if random.random() < 0.30:
                    async def delayed_follow_up():
                        late_delay = random.uniform(6.0, 14.0)
                        await asyncio.sleep(late_delay)

                        if pending_catches > 0:
                            print("🚫 [Stealth] Cancelled follow-up: Priority catch in queue.")
                            return

                        if is_paused or is_afk or is_incense_mode:
                            return

                        follow_up_msgs = [
                            "ok", "shine", f"<@{POKETWO_BOT_ID}> i l", "hmm",
                            f"<@{POKETWO_BOT_ID}> sh", "damn", "oh", "sheesh", "wow", "ggs"
                        ]
                        chosen_msg = random.choice(follow_up_msgs)
                        msg_typing_delay = len(chosen_msg) * random.uniform(0.04, 0.08)

                        async with bot_action_lock:
                            if is_paused or is_afk or is_incense_mode or pending_catches > 0:
                                return

                            async with message.channel.typing():
                                await asyncio.sleep(msg_typing_delay)

                            await message.channel.send(chosen_msg)
                            print(f"💬 [Stealth] Sent late follow-up message: '{chosen_msg}'")

                    bot.loop.create_task(delayed_follow_up())

            finally:
                pending_catches -= 1

        await catch_queue.add(process_notification)


if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "PASTE_YOUR_ACTUAL_TOKEN_HERE":
        print("❌ ERROR: DISCORD_TOKEN is missing! Set it in your .env file or paste it inside config.py.")
        sys.exit(1)
    bot.run(DISCORD_TOKEN)
