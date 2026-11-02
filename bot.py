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

# Maximum seconds before an incense spawn despawns (20s window with 0.8s network buffer)
INCENSE_DESPAWN_LIMIT = 19.2

# --- Global State & Locks ---
is_paused = False
is_afk = False
is_incense_mode = False
active_incense_guild: discord.Guild | None = None

# Task tracker for ongoing channel 1-10 command loops (buy / p / r)
incense_cmd_task: asyncio.Task | None = None

# Tracks active catch tasks per channel (channel_name -> asyncio.Task) for wave replacement
incense_tasks: dict[str, asyncio.Task] = {}

# Ensures the bot can NEVER type/send two messages at the exact same time across any channel
bot_action_lock = asyncio.Lock()

# Tracks how many pokemon are currently queued or being caught
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
    Sends '<@POKETWO_BOT_ID> <subcommand>' across channels 1 to 10 in the given server
    one by one with a random 1.0 to 2.0 second delay.
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

        delay = random.uniform(1.0, 2.0)
        await asyncio.sleep(delay)

        async with bot_action_lock:
            if abort_if_paused and (is_paused or not is_incense_mode):
                print(f"⏹️ [Incense] '{subcommand}' sequence halted before sending.")
                return

            cmd_msg = f"<@{POKETWO_BOT_ID}> {subcommand}"
            async with channel.typing():
                await asyncio.sleep(len(cmd_msg) * random.uniform(0.03, 0.06))

            await channel.send(cmd_msg)
            print(f"✅ [Incense] Sent '{subcommand}' in #{channel.name} (Delay: {int(delay * 1000)}ms)")

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


async def catch_incense_spawn(
    channel: discord.abc.Messageable,
    channel_name: str,
    pokemon_name: str,
    spawn_timestamp: float,
) -> None:
    """
    Direct, non-queued incense catcher with per-channel task replacement.
    Uses bot_action_lock so zero messages conflict across channels.
    """
    global pending_catches
    pending_catches += 1
    try:
        if is_paused or not is_incense_mode:
            return

        # Strictly serialize across all 10 channels so two channels never send at once
        async with bot_action_lock:
            if is_paused or not is_incense_mode:
                return

            # Check if the spawn is about to hit the 20s despawn limit
            elapsed_before_gap = time.monotonic() - spawn_timestamp
            if elapsed_before_gap >= INCENSE_DESPAWN_LIMIT:
                print(
                    f"⏳ [Incense] Skipped {pokemon_name} in #{channel_name} "
                    f"(Expired: {elapsed_before_gap:.1f}s >= 20s despawn window)"
                )
                return

            # Random gap between 0.70s and 1.25s
            catch_gap = random.uniform(0.70, 1.25)
            await asyncio.sleep(catch_gap)

            if is_paused or not is_incense_mode:
                return

            final_name = pokemon_name.lower() if random.random() < 0.70 else pokemon_name
            cmd = random.choice(["c", "catch"])
            final_message = f"<@{POKETWO_BOT_ID}> {cmd} {final_name}"

            # Fast typing burst (0.15s to 0.30s)
            typing_delay = random.uniform(0.15, 0.30)
            async with channel.typing():
                await asyncio.sleep(typing_delay)

            await channel.send(final_message)
            total_elapsed = time.monotonic() - spawn_timestamp
            print(
                f"🏓 [Incense] Caught {final_name} in #{channel_name} "
                f"(Gap: {int(catch_gap * 1000)}ms | Wave Time: {total_elapsed:.2f}s/20s)"
            )
    except asyncio.CancelledError:
        print(f"🚫 [Incense] Task in #{channel_name} cancelled (superseded by new spawn).")
        raise
    finally:
        pending_catches -= 1


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} | Ultimate Stealth Mode + Wave Incense Catcher Active.")
    print(f"🎯 [Stealth] Next forced miss is scheduled in {next_miss_target} pings.")


@bot.event
async def on_message(message: discord.Message) -> None:
    global is_paused, is_afk, is_incense_mode, active_incense_guild
    global ping_counter, next_miss_target, pending_catches

    msg_content = message.content.strip().lower()

    # 1. Handle own commands
    if message.author.id == bot.user.id:
        if msg_content == "!pause":
            is_paused = True
            print("⏸️ Bot manually paused.")
            return

        if msg_content == "!resume":
            is_paused = False
            mode_str = "INCENSE MODE" if is_incense_mode else "NORMAL MODE"
            print(f"▶️ Bot manually resumed in {mode_str}.")
            return

        if msg_content == "!inc_start":
            is_incense_mode = True
            is_paused = False
            is_afk = False
            if message.guild:
                active_incense_guild = message.guild
                print("🔥 [Mode Switch] INCENSE MODE STARTED. Buying incense in channels 1-10 & wave catching enabled.")
                start_incense_channel_task(
                    message.guild, "inc buy -y", "Buy Incense", abort_if_paused=True
                )
            return

        if msg_content == "!inc_pause":
            is_paused = True
            target_guild = message.guild or active_incense_guild
            print("⏸️ [Incense] !inc_pause triggered. Pausing catches and sending 'inc p' to channels 1-10...")
            if target_guild:
                start_incense_channel_task(
                    target_guild, "inc p", "Pause Incense", abort_if_paused=False
                )
            return

        if msg_content == "!inc_resume":
            is_incense_mode = True
            is_paused = False
            is_afk = False
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
    # 4A. INCENSE MODE CATCHING LOGIC (Per-Channel Task Replacement)
    # =========================================================================
    if is_incense_mode:
        channel_name = getattr(message.channel, "name", "")
        if message.author.id == INCENSE_HELPER_BOT_ID and channel_name in INCENSE_CHANNELS:
            if message.guild:
                active_incense_guild = message.guild

            pokemon_name = get_pokemon_name(message.content)
            if not pokemon_name:
                return

            # Cancel any existing active catch task for this specific channel
            if channel_name in incense_tasks and not incense_tasks[channel_name].done():
                print(f"🔄 [Incense Wave] New spawn in #{channel_name}! Cancelling previous unfinished catch task.")
                incense_tasks[channel_name].cancel()

            spawn_timestamp = time.monotonic()
            print(f"⚡ [Incense Wave] Spawn detected in #{channel_name}: {pokemon_name}")

            # Create and track the new catch task for this channel
            task = bot.loop.create_task(
                catch_incense_spawn(
                    channel=message.channel,
                    channel_name=channel_name,
                    pokemon_name=pokemon_name,
                    spawn_timestamp=spawn_timestamp,
                )
            )
            incense_tasks[channel_name] = task
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
