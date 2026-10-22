"""Dalgona Candy: Sequential group shape rounds, private numbered-grid input."""
from __future__ import annotations
import asyncio
import logging
import random
import time
from telegram.error import BadRequest, RetryAfter, TelegramError
from .. import runtime, ui
from ..manager import safe_answer, safe_edit, game_finished, refresh_lobby_scoreboard
from ..repository import get_match, save_match, alive, update_dalgona_tap
log = logging.getLogger(__name__)
GROUP_SHAPES = [
    ("Circle", 3, "⭕"),
    ("Triangle", 4, "🔺"),
    ("Star", 5, "⭐"),
    ("Umbrella", 6, "☂️"),
]
FLASH_SECONDS = 1
DM_READY_SECONDS = 5
PREPARATION_SECONDS = 15


def _state_lock(mid: str) -> asyncio.Lock:
    """Return the per-match Dalgona mutation lock."""
    state = runtime.get(str(mid))
    lock = state.get('dalgona_state_lock')
    if lock is None:
        lock = state['dalgona_state_lock'] = asyncio.Lock()
    return lock


def _input_seconds(sequence_length: int) -> int:
    """Dynamically calculate time based on sequence length."""
    return max(8, min(25, 5 + int(sequence_length * 1.5)))


def _player_list_html(players: list[dict]) -> str:
    """Generate hyperlinked player list for Msg-1."""
    links = []
    for p in players:
        if p.get('alive'):
            uid = p.get('id')
            name = ui.html.escape(p.get('name') or str(uid))
            links.append(f'<a href="tg://user?id={uid}">{name}</a>')
    return ', '.join(links) if links else 'None'


def _group_details(match: dict, seconds_left: int=None) -> str:
    """Render the dynamic content for the persistent group card (Msg-1)."""
    game = match.get('game') or {}
    players = match.get('players', [])
    alive_players = [p for p in players if p.get('alive')]
    phase = game.get('sequence_phase')
    shape_idx = game.get('current_shape_index', 0)
    text = []
    if phase == 'preparation':
        text.append(ui.round_waiting_panel(
            'dalgona',
            PREPARATION_SECONDS if seconds_left is None else seconds_left,
            total_seconds=PREPARATION_SECONDS,
        ))
        text.append(f'👥 <b>Players ({len(alive_players)}):</b>\n{_player_list_html(alive_players)}')
    elif phase == 'showing':
        shape_name, _, emoji = GROUP_SHAPES[shape_idx]
        text.append('🧠 <b>Watch the flashing grid closely!</b>')
        text.append(f'Current Shape: <b>{emoji} {shape_name}</b>')
    elif phase == 'transition':
        text.append('🚶 <b>Switch to your DM!</b> The grid will appear shortly.')
    elif phase in ('ready', 'input'):
        text.append('🎯 <b>Perform the sequence in your DM now!</b>')
    elif phase == 'resolved':
        text.append('📊 <b>Results updated!</b> Next round starting soon…')
    return '\n\n'.join(text)


async def _refresh_group(context, mid: str, event: str | None=None, finished: bool=False, seconds_left: int=None) -> None:
    match = await get_match(mid)
    if not match or match.get('stage') != 'dalgona':
        return
    game = match.setdefault('game', {})
    if event:
        game['last_event'] = event
        await save_match(match)
    phase = game.get('sequence_phase')
    update_round_card = bool(event or finished or phase in {'preparation', 'resolved'})
    if update_round_card:
        await ui.refresh_round_announcement(context.bot, match, 'dalgona', details=_group_details(match, seconds_left), event=game.get('last_event') if event else None, finished=finished)
    if event or finished:
        await refresh_lobby_scoreboard(context, mid, force=finished)


async def _set_board(context, match: dict, shape_name: str, emoji: str, sequence_length: int, first_cell: int) -> int:
    """Send the temporary flashing grid, showing the first tile immediately."""
    markup = ui.dalgona_display_keyboard(str(match['_id']), sequence_length, active_cell=first_cell)
    text = f"🍪 <b>DALGONA · {emoji} {shape_name.upper()}</b>\n\nWatch all {sequence_length} highlighted tiles in order.\nAfter the grid disappears, use the next message's DM button to repeat the pattern."
    message = await context.bot.send_message(chat_id=match['chat_id'], text=text, parse_mode='HTML', reply_markup=markup, disable_web_page_preview=True)
    return message.message_id


async def _edit_board_markup(context, chat_id: int, board_id: int, mid: str, seq_len: int, active_cell: int | None) -> bool:
    markup = ui.dalgona_display_keyboard(mid, seq_len, active_cell=active_cell)
    while True:
        try:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=board_id, reply_markup=markup)
            return True
        except RetryAfter as exc:
            delay = getattr(exc, 'retry_after', 1)
            if hasattr(delay, 'total_seconds'):
                delay = delay.total_seconds()
            delay = max(0.0, float(delay))
            log.warning('Dalgona flash rate-limited for cell %s; retrying the same tile in %.2fs', active_cell, delay)
            await asyncio.sleep(delay + 0.1)
        except BadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return True
            log.warning('Dalgona flash update failed for cell %s: %s', active_cell, exc)
            return False
        except TelegramError as exc:
            log.warning('Dalgona flash update failed for cell %s: %s', active_cell, exc)
            return False


def _new_sequence(length: int) -> list[int]:
    """Generate a sequence without adjacent repeats that would look like a stuck flash."""
    sequence: list[int] = []
    for _ in range(length):
        choices = [cell for cell in range(1, 10) if not sequence or cell != sequence[-1]]
        sequence.append(random.choice(choices))
    return sequence


async def _delete_previous_dm_prompt(context, mid: str) -> None:
    """Keep only the current pattern's group-to-DM instruction below the game card."""
    message_id = None
    async with _state_lock(mid):
        match = await get_match(mid)
        if not match:
            return
        game = match.get('game') or {}
        message_id = game.get('dm_prompt_message_id')
        if not message_id:
            return
        game['dm_prompt_message_id'] = None
        await save_match(match)
    try:
        await context.bot.delete_message(chat_id=match['chat_id'], message_id=message_id)
    except Exception:
        log.debug('Could not remove previous Dalgona DM prompt %s', message_id, exc_info=True)


async def _send_dm_prompt(context, mid: str, shape_name: str, emoji: str, sequence_length: int) -> None:
    """Post a clear group CTA with a URL button that opens the bot's private chat."""
    bot_username = getattr(context.bot, 'username', None)
    if not bot_username:
        try:
            bot_username = (await context.bot.get_me()).username
        except Exception:
            log.exception('Could not resolve bot username for Dalgona DM button')
    current = await get_match(mid)
    if not current or current.get('stage') != 'dalgona':
        return
    markup = ui.open_dm_keyboard(bot_username, label='📩 OPEN BOT DM & PLAY')
    text = f"✅ <b>{emoji} {shape_name.upper()} PATTERN SHOWN</b>\n\n<b>Now open the bot's private chat.</b>\nTap the numbered tiles <b>1–9</b> in the same order ({sequence_length} taps).\n❌ One wrong tile breaks your candy. Your controls activate shortly."
    message = await context.bot.send_message(chat_id=current['chat_id'], text=text, parse_mode='HTML', reply_markup=markup, disable_web_page_preview=True)
    async with _state_lock(mid):
        latest = await get_match(mid)
        if latest and latest.get('stage') == 'dalgona':
            latest.setdefault('game', {})['dm_prompt_message_id'] = message.message_id
            await save_match(latest)
        else:
            try:
                await context.bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
            except Exception:
                pass


async def begin(context, mid: str) -> None:
    """Initialize the Dalgona round."""
    match = await get_match(mid)
    if not match or match.get('stage') != 'dalgona':
        return
    players = alive(match)
    player_states = {str(p['id']): {'progress': 0, 'phase': 'preparation', 'dm_mid': None, 'done': False} for p in players}
    match['game'] = {'players': player_states, 'last_event': None, 'round_finished': False, 'current_shape_index': random.randint(0, len(GROUP_SHAPES) - 1), 'current_sequence': [], 'sequence_phase': 'preparation', 'input_seconds': None, 'dm_prompt_message_id': None}
    await save_match(match)
    await ui.send_round_announcement(context.bot, match, 'dalgona', details=_group_details(match, PREPARATION_SECONDS))
    for p in players:
        try:
            msg = await context.bot.send_message(chat_id=p['id'], text='🍪 <b>Dalgona has started!</b>\n\nWatch the group chat for the pattern.', parse_mode='HTML')
            match['game']['players'][str(p['id'])]['dm_mid'] = msg.message_id
        except Exception:
            p['alive'] = False
            match['game']['players'][str(p['id'])]['phase'] = 'dead'
    await save_match(match)
    runtime.add_task(mid, asyncio.create_task(_run_game(context, mid)))


async def _run_game(context, mid: str) -> None:
    """Coordinate each Dalgona pattern without skipping failed flash edits."""
    pending_pattern: tuple[int, list[int]] | None = None
    flash_restarts: dict[int, int] = {}
    try:
        checkpoints = [15, 10, 5]
        for index, remaining in enumerate(checkpoints):
            await _refresh_group(context, mid, seconds_left=remaining)
            next_remaining = checkpoints[index + 1] if index + 1 < len(checkpoints) else 0
            await asyncio.sleep(remaining - next_remaining)
        while True:
            await _delete_previous_dm_prompt(context, mid)
            async with _state_lock(mid):
                match = await get_match(mid)
                if not match or match.get('stage') != 'dalgona':
                    return
                game = match.get('game') or {}
                if game.get('round_finished'):
                    return
                shape_idx = int(game.get('current_shape_index', 0))
                alive_players = [player for player in match.get('players', []) if player.get('alive', True)]
                if not alive_players:
                    await _finish(context, mid, [])
                    return
                shape_name, seq_len, emoji = GROUP_SHAPES[shape_idx]
                if pending_pattern and pending_pattern[0] == shape_idx:
                    sequence = list(pending_pattern[1])
                else:
                    sequence = _new_sequence(seq_len)
                game.update({'current_sequence': sequence, 'sequence_phase': 'showing', 'input_seconds': None})
                player_states = game.setdefault('players', {})
                for player in alive_players:
                    state = player_states.get(str(player['id']))
                    if not isinstance(state, dict):
                        player['alive'] = False
                        continue
                    state.update({'progress': 0, 'phase': 'showing', 'done': False})
                await save_match(match)
            alive_players = [player for player in match.get('players', []) if player.get('alive', True)]
            if not alive_players:
                await _finish(context, mid, [])
                return
            await _refresh_group(context, mid)
            try:
                board_id = await _set_board(context, match, shape_name, emoji, seq_len, first_cell=sequence[0])
            except RetryAfter as exc:
                delay = getattr(exc, 'retry_after', 1)
                await asyncio.sleep(max(0.0, float(getattr(delay, 'total_seconds', lambda: delay)())) + 0.1)
                pending_pattern = (shape_idx, sequence)
                continue
            flash_ok = True
            next_flash_at = time.monotonic() + FLASH_SECONDS
            for cell in sequence[1:]:
                delay = next_flash_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                flash_started = time.monotonic()
                if not await _edit_board_markup(context, match['chat_id'], board_id, mid, seq_len, cell):
                    flash_ok = False
                    break
                next_flash_at = flash_started + FLASH_SECONDS
            if not flash_ok:
                try:
                    await context.bot.delete_message(chat_id=match['chat_id'], message_id=board_id)
                except TelegramError:
                    pass
                attempts = flash_restarts.get(shape_idx, 0) + 1
                flash_restarts[shape_idx] = attempts
                pending_pattern = (shape_idx, sequence)
                if attempts <= 2:
                    await _refresh_group(context, mid, event=f'⚠️ Telegram could not update the {shape_name} grid. The same pattern will restart.')
                    await asyncio.sleep(2.0)
                    continue
                latest = await get_match(mid)
                survivors = [player['id'] for player in (latest or {}).get('players', []) if player.get('alive', True)]
                await _finish(context, mid, survivors)
                return
            pending_pattern = None
            flash_restarts.pop(shape_idx, None)
            try:
                await context.bot.delete_message(chat_id=match['chat_id'], message_id=board_id)
            except TelegramError:
                pass
            input_seconds = _input_seconds(seq_len)
            async with _state_lock(mid):
                match = await get_match(mid)
                if not match or match.get('stage') != 'dalgona':
                    return
                game = match.setdefault('game', {})
                game['sequence_phase'] = 'ready'
                await save_match(match)
            await _refresh_group(context, mid)
            await _send_dm_prompt(context, mid, shape_name, emoji, seq_len)
            alive_players = [player for player in match.get('players', []) if player.get('alive', True) and isinstance((match.get('game', {}).get('players', {}) or {}).get(str(player.get('id'))), dict)]
            dm_actions = [safe_edit(context.bot, player['id'], match['game']['players'][str(player['id'])].get('dm_mid'), f'🍪 <b>{emoji} {shape_name.upper()}</b>\n\nGet ready... controls open in {DM_READY_SECONDS}s!', reply_markup=ui.dalgona_keyboard(mid, shape_idx)) for player in alive_players]
            await asyncio.gather(*dm_actions, return_exceptions=True)
            await asyncio.sleep(DM_READY_SECONDS)
            async with _state_lock(mid):
                match = await get_match(mid)
                if not match or match.get('stage') != 'dalgona':
                    return
                game = match.setdefault('game', {})
                game['sequence_phase'] = 'input'
                game['input_seconds'] = input_seconds
                player_states = game.get('players', {}) or {}
                for player in alive_players:
                    state = player_states.get(str(player['id']))
                    if isinstance(state, dict):
                        state['phase'] = 'input'
                await save_match(match)
                event = asyncio.Event()
                rt = runtime.get(mid)
                rt['dalgona_wave_event'] = event
                rt['dalgona_tracker'] = {'round_index': shape_idx, 'sequence': sequence, 'players': {str(player['id']): {'progress': 0, 'done': False, 'alive': True, 'dm_mid': player_states.get(str(player['id']), {}).get('dm_mid')} for player in alive_players if isinstance(player_states.get(str(player['id'])), dict)}}
            input_actions = [safe_edit(context.bot, player['id'], match['game']['players'][str(player['id'])].get('dm_mid'), f'🚦 <b>GO!</b> You have {input_seconds}s to repeat the {seq_len}-tap pattern.', reply_markup=ui.dalgona_keyboard(mid, shape_idx)) for player in alive_players if isinstance(match['game'].get('players', {}).get(str(player['id'])), dict)]
            await asyncio.gather(*input_actions, return_exceptions=True)
            try:
                await asyncio.wait_for(event.wait(), timeout=input_seconds)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                log.exception(f'Unexpected error during Dalgona timer: {e}')
            timed_out_count = 0
            async with _state_lock(mid):
                match = await get_match(mid)
                if not match or match.get('stage') != 'dalgona':
                    return
                game = match.setdefault('game', {})
                player_states = game.get('players', {}) or {}
                for player in match.get('players', []):
                    if not player.get('alive', True):
                        continue
                    state = player_states.get(str(player.get('id')))
                    if not isinstance(state, dict) or not state.get('done'):
                        player['alive'] = False
                        if isinstance(state, dict):
                            state['phase'] = 'dead'
                            dm_mid = state.get('dm_mid')
                        else:
                            dm_mid = None
                        timed_out_count += 1
                        if dm_mid:
                            asyncio.create_task(safe_edit(context.bot, player['id'], dm_mid, "💀 <b>TIME'S UP!</b> Candy broken."))
                game['sequence_phase'] = 'resolved'
                rt = runtime.get(mid)
                if rt:
                    rt['dalgona_wave_event'] = None
                    rt.pop('dalgona_tracker', None)
                await save_match(match)
            survivors = [player for player in match.get('players', []) if player.get('alive', True)]
            result_str = f'<b>{emoji} {shape_name} Results:</b>\n✅ {len(survivors)} Survived\n💀 {timed_out_count} Eliminated'
            
            await _delete_previous_dm_prompt(context, mid)
            
            survivor_ids = [player["id"] for player in survivors if player.get("id") is not None]
            await _finish(context, mid, survivor_ids, event=result_str)
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception('Dalgona game loop crashed (match=%s)', mid)
        try:
            latest = await get_match(mid)
            if latest and latest.get('stage') == 'dalgona':
                survivors = [player['id'] for player in latest.get('players', []) if player.get('alive', True) and player.get('id') is not None]
                await _finish(context, mid, survivors)
        except Exception:
            log.exception('Could not finalize Dalgona after loop failure (match=%s)', mid)


async def on_press(update, context) -> None:
    """Validate callbacks and safely process private-grid taps using fast in-memory tracking."""
    query = getattr(update, 'callback_query', None)
    if not query:
        return
    parts = (query.data or '').split(':')
    if len(parts) != 4 or parts[0] != 'dal':
        await safe_answer(query, 'Invalid Dalgona button.', True)
        return
    try:
        round_index = int(parts[2])
        cell = int(parts[3])
    except ValueError:
        await safe_answer(query, 'Invalid tile. Please use the numbered grid.', True)
        return
    if not 1 <= cell <= 9:
        await safe_answer(query, 'Invalid tile. Choose a number from 1 to 9.', True)
        return
    mid = parts[1]
    uid = query.from_user.id
    uid_str = str(uid)
    rt = runtime.get(mid)
    tracker = rt.get('dalgona_tracker') if rt else None
    if not tracker:
        await safe_answer(query, 'Controls are not active yet or round expired.', True)
        return
    if round_index != tracker['round_index']:
        await safe_answer(query, 'These buttons are from an earlier pattern.', True)
        return
    player_state = tracker['players'].get(uid_str)
    if not player_state or not player_state['alive']:
        await safe_answer(query, 'You are eliminated or not in this match.', True)
        return
    player_lock = rt.setdefault('dalgona_player_locks', {}).setdefault(uid_str, asyncio.Lock())
    async with player_lock:
        if not player_state['alive']:
            await safe_answer(query, 'You are already eliminated.', True)
            return
        if player_state['done']:
            await safe_answer(query, 'You have already cleared this pattern. ✅', True)
            return
        sequence = tracker['sequence']
        progress = player_state['progress']
        if progress < 0 or progress >= len(sequence):
            await safe_answer(query, 'Your pattern state has expired.', True)
            return
        if cell != sequence[progress]:
            player_state['alive'] = False
            await safe_answer(query, '❌ Wrong tile! Your candy broke.', True)
            await safe_edit(context.bot, uid, player_state.get('dm_mid'), '💥💀 <b>ELIMINATED!</b> You tapped the wrong tile.')
            await update_dalgona_tap(mid, uid, {'alive': False}, {'progress': progress, 'done': False, 'phase': 'dead'})
        else:
            player_state['progress'] += 1
            if player_state['progress'] >= len(sequence):
                player_state['done'] = True
                await safe_answer(query, '✅ Pattern cleared!', True)
                await safe_edit(context.bot, uid, player_state.get('dm_mid'), '✅ <b>Pattern cleared!</b> Wait in the group for the next round.')
                await update_dalgona_tap(mid, uid, {'alive': True}, {'progress': player_state['progress'], 'done': True, 'phase': 'input'})
            else:
                await safe_answer(query, f"✅ {player_state['progress']}/{len(sequence)}")
                await update_dalgona_tap(mid, uid, {'alive': True}, {'progress': player_state['progress'], 'done': False, 'phase': 'input'})
    alive_players = [p for p in tracker['players'].values() if p['alive']]
    all_alive_done = all((p['done'] for p in alive_players))
    if not alive_players or all_alive_done:
        event = rt.get('dalgona_wave_event')
        if event:
            event.set()


async def _finish(context, mid: str, survivors: list[int], event: str | None = None) -> None:
    """Finalize Dalgona, publish results, and continue the manager lifecycle."""
    await _delete_previous_dm_prompt(context, mid)
    match = await get_match(mid)
    if not match or match.get('stage') != 'dalgona':
        return
    game = match.setdefault('game', {})
    game['round_finished'] = True
    if event:
        game['last_event'] = event
    existing_entrants = {str(uid) for uid in match.get('current_round_player_ids') or []}
    if not existing_entrants:
        state_players = game.get('players') or {}
        state_ids = set(state_players.keys()) if isinstance(state_players, dict) else set()
        entrants = [player.get('id') for player in match.get('players', []) if player.get('id') is not None and str(player.get('id')) in state_ids]
        if entrants:
            match['current_round_player_ids'] = entrants
    await save_match(match)
    try:
        await ui.refresh_round_announcement(
            context.bot, match, 'dalgona', 
            details=_group_details(match), 
            event=game.get('last_event'), 
            finished=True
        )
    except Exception:
        log.exception('Could not edit Dalgona round announcement (match=%s)', mid)
    await game_finished(context, mid, survivors, 'Dalgona Candy')
