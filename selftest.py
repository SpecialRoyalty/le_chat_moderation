import re
from pathlib import Path
bot=Path('bot.py').read_text(encoding='utf-8')
def contains(text,pattern):
    return bool(re.search(rf'(?<![a-z0-9]){re.escape(pattern.lower().strip())}(?![a-z0-9])',text.lower(),re.I))
assert contains('tu as son snap','snap')
assert contains('hi come in dm','hi') and contains('hi come in dm','come') and contains('hi come in dm','dm')
assert not contains('Mathias Baret','hi')
assert not contains('scp_user','cp')
assert contains('user-cp-test','cp')
assert 'grace_presidentielle' not in bot and 'grace_ministerielle' not in bot
h=bot[bot.index('async def handle_group_message'):bot.index('async def chat_member_update')]
pos_hash=h.index('any_banned')
pos_hard=h.index("SELECT word FROM banned_words_hard")
pos_words=h.index("SELECT word FROM banned_words')")
pos_links=h.index('has_external_link')
assert pos_hash < pos_hard < pos_words < pos_links
assert 'return uid in TRUSTED_IDS or uid in SUPER_TRUSTED_IDS or is_admin(uid)' in bot
for s in ['MSG_GENERIC_FORBIDDEN','MSG_FAKE_COMMAND','MSG_PARTICIPATION_REQUIRED']: assert s+'=' in bot or s+' =' in bot
print('SELFTEST OK')

assert 'nonparticipant_seen' in bot
assert 'open_session_admin' in bot
assert 'build_system_info' in bot
assert 'run_admin_autotest' in bot
