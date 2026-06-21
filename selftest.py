from pathlib import Path
bot=Path('bot.py').read_text(encoding='utf-8')

def part(name):
    s=bot.index(f'async def {name}')
    e=bot.find('\nasync def ', s+1)
    return bot[s:e if e!=-1 else len(bot)]

def test_v15():
    assert 'FINAL_CLEAN_V15_CLOSED_SILENT_REFERRAL_10MIN' in bot
    assert 'closed_session_restrict_silent' in bot
    assert 'closed_session_ban_silent' in bot
    c=part('closed_session_block')
    assert 'participation_warning' not in c
    assert 'CLOSED SESSION DELETE SILENT' in c
    assert 'punish_word' not in c
    assert 'REFERRAL 10MIN SCHEDULE' in bot
    assert 'validate_referral_after_10min' in bot
    assert 'REFERRAL VALIDATED 10MIN' in bot
    assert 'grace_presidentielle' not in bot
    assert 'grace_ministerielle' not in bot

if __name__=='__main__':
    test_v15()
    print('V15 SELFTEST OK')
