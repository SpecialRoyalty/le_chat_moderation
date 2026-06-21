from pathlib import Path
bot=Path('bot.py').read_text(encoding='utf-8')

def part(name):
    s=bot.index(f'async def {name}')
    e=bot.find('\nasync def ', s+1)
    return bot[s:e if e!=-1 else len(bot)]

def test_v12():
    assert 'FINAL_CLEAN_V12_KICK_NOTICES_CLEANUP' in bot
    assert 'nonparticipant_kick_active_session_id' in bot
    assert 'NONPARTICIPANT KICK NOTICE KEPT' in bot
    assert 'NONPARTICIPANT KICK CLEANUP' in bot
    h=part('handle_group_message')
    assert 'left_chat_member' in h
    assert h.index('NONPARTICIPANT KICK NOTICE KEPT') < h.index('JOIN_LEFT SERVICE DELETE')
    k=part('kick_nonparticipants_public')
    assert 'nonparticipant_kick_active_session_id' in k
    c=part('cleanup_nonparticipant_kick_messages')
    assert 'delete_safe' in c
    assert 'grace_presidentielle' not in bot
    assert 'grace_ministerielle' not in bot

if __name__=='__main__':
    test_v12()
    print('V12 SELFTEST OK')
