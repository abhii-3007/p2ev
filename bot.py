import asyncio
import random
import discord

from config import DISCORD_TOKEN, POKETWO_BOT_ID, HELPER_BOT_IDS
from services.parser import extract_pokemon_name
from services.queue import CatchQueue
from services.stealth import simulate_typo

# discord.py-self does not require intents for user accounts
bot = discord.Client()
catch_queue = CatchQueue()

# --- Incense Configuration ---
INCENSE_HELPER_BOT_ID = 1307910235737948252
INCENSE_CHANNELS = {str(i) for i in range(1, 11)}

# --- Global State & Locks ---
is_paused = False
is_afk = False
is_incense_mode = False

# This lock ensures the bot can never type/send two different messages at the exact same time
bot_action_lock = asyncio.Lock()

# Tracks how many pokemon are currently queued or being caught
pending_catches = 0

# --- Smart Miss Counter Variables ---
ping_counter = 0
next_miss_target = random.randint(10, 15)


async def afk_timer(afk_seconds: int) -> None:
    global is_afk
    minutes = afk_seconds // 60
    seconds = afk_seconds % 60
    print(f"🚶 [Stealth] Taking a bathroom break. AFK for {minutes} minutes and {seconds} seconds.")

    await asyncio.sleep(afk_seconds)

    is_afk = False
    print("🔙 [Stealth] Back at the keyboard. Resuming auto-catch automatically!")


async def buy_incense_sequence(guild: discord.Guild) -> None:
    """Buys incense sequentially in channels named 1 to 10 with a 1-2s delay."""
    print("🔥 [Incense] Starting incense purchase sequence across channels 1 to 10...")
    for i in range(1, 11):
        if is_paused or not is_incense_mode:
            print("⏹️ [Incense] Purchase sequence halted (bot paused or switched to normal mode).")
            return

        channel = discord.utils.get(guild.text_channels, name=str(i))
        if channel is None:
            print(f"⚠️ [Incense] Channel '{i}' not found in this server. Skipping.")
            continue

        delay = random.uniform(1.0, 2.0)
        await asyncio.sleep(delay)

        async with bot_action_lock:
            if is_paused or not is_incense_mode:
                print("⏹️ [Incense] Purchase sequence halted before sending.")
                return

            cmd_msg = f"<@{POKETWO_BOT_ID}> inc buy -y"
            async with channel.typing():
                await asyncio.sleep(len(cmd_msg) * random.uniform(0.03, 0.06))

            await channel.send(cmd_msg)
            print(f"✅ [Incense] Bought incense in #{channel.name} (Delay: {int(delay * 1000)}ms)")

    print("🎉 [Incense] Finished buying incense in channels 1 to 10!")


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} | Ultimate Stealth Mode + Async Queue Active.")
    print(f"🎯 [Stealth] Next forced miss is scheduled in {next_miss_target} pings.")


@bot.event
async def on_message(message: discord.Message) -> None:
    global is_paused, is_afk, is_incense_mode
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

        if msg_content == "!inc":
            is_incense_mode = True
            is_afk = False  # Clear AFK state if transitioning into Incense Mode
            print("🔥 [Mode Switch] INCENSE MODE ACTIVATED. Normal ping-catching is now OFF.")
            if message.guild:
                bot.loop.create_task(buy_incense_sequence(message.guild))
            return

        if msg_content == "!normal":
            is_incense_mode = False
            print("🌿 [Mode Switch] NORMAL MODE ACTIVATED. Incense mode is now OFF.")
            return

        return

    # 2. Safety conditions (Captcha - pauses in BOTH modes)
    if (
        message.author.id == POKETWO_BOT_ID
        and "Please tell us you're human!" in message.content
        and str(bot.user.id) in message.content
    ):
        is_paused = True
        print("⚠️ Captcha detected targeting YOUR account. Script paused immediately!")
        return

    # 3. Shiny Catch Sequence (Pauses ONLY in Normal Mode; continues in Incense Mode)
    if (
        message.author.id == POKETWO_BOT_ID
        and "These colors seem unusual..." in message.content
        and str(bot.user.id) in message.content
    ):
        if is_incense_mode:
            print(f"✨ [Incense] Shiny caught in #{getattr(message.channel, 'name', 'unknown')}! Continuing without pausing...")
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

    # Stop processing if paused (or if AFK in normal mode)
    if is_paused or (is_afk and not is_incense_mode):
        return

    # =========================================================================
    # 4A. INCENSE MODE CATCHING LOGIC (Channels 1-10, Helper ID: 1307910235737948252)
    # =========================================================================
    if is_incense_mode:
        channel_name = getattr(message.channel, "name", "")
        if message.author.id == INCENSE_HELPER_BOT_ID and channel_name in INCENSE_CHANNELS:
            pokemon_name = extract_pokemon_name(message.content)
            if not pokemon_name:
                return

            pending_catches += 1
            print(f"📥 [Incense] #{channel_name} queued: {pokemon_name} (Pending: {pending_catches})")

            async def process_incense_catch() -> None:
                global pending_catches
                try:
                    if is_paused or not is_incense_mode:
                        return

                    # Random 1 to 2 second gap per spawn catch
                    catch_gap = random.uniform(1.0, 2.0)
                    await asyncio.sleep(catch_gap)

                    async with bot_action_lock:
                        if is_paused or not is_incense_mode:
                            return

                        final_name = pokemon_name.lower() if random.random() < 0.70 else pokemon_name
                        cmd = random.choice(["c", "catch"])
                        final_message = f"<@{POKETWO_BOT_ID}> {cmd} {final_name}"

                        typing_delay = len( pokemon_name) * random.uniform(0.03, 0.06)
                        async with message.channel.typing():
                            await asyncio.sleep(typing_delay)

                        await message.channel.send(final_message)
                        print(f"🏓 [Incense] Caught {final_name} in #{channel_name} (Gap: {int(catch_gap * 1000)}ms)")
                finally:
                    pending_catches -= 1

            await catch_queue.add(process_incense_catch)
        return

    # =========================================================================
    # 4B. NORMAL MODE CATCHING LOGIC (Standard Ping-Reply Catching)
    # =========================================================================
    if message.author.id in HELPER_BOT_IDS and str(bot.user.id) in message.content:

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

        pokemon_name = extract_pokemon_name(message.content)
        if not pokemon_name:
            return

        # Increase the queue counter right before adding it
        pending_catches += 1
        print(f"\n📥 Ping {ping_counter}/{next_miss_target} added to the queue: {pokemon_name} (Total Pending: {pending_catches})")

        async def process_notification() -> None:
            global pending_catches
            try:
                if is_paused or is_afk or is_incense_mode:
                    return

                print(f"⚙️ Processing queued catch for: {pokemon_name}")

                # 1. Read delay
                read_delay = random.uniform(0.4, 0.8)
                if random.random() < 0.10:
                    distraction_time = random.uniform(2.0, 5.0)
                    read_delay += distraction_time
                    print(f"[Stealth] Distraction triggered. Delaying reaction by {int(distraction_time * 1000)}ms")

                await asyncio.sleep(read_delay)

                # 2. Start Typing / Catching
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

                # 3. Typo Correction (if applicable)
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

                # 4. Late Follow-up Message (Runs as a background task)
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
    bot.run(DISCORD_TOKEN)