from pathlib import Path
bot=Path('bot.py').read_text(encoding='utf-8')

def part(name):
    s=bot.index(f'async def {name}')
    e=bot.find('\nasync def ', s+1)
    return bot[s:e if e!=-1 else len(bot)]

def test_v16():
    assert 'FINAL_CLEAN_V16_CLOSED_LOCKDOWN_REFERRAL_COUNTER' in bot
    assert 'has_any_content_v16' in bot
    assert 'has_any_media_or_attachment_v16' in bot
    c=part('closed_session_block')
    assert 'CLOSED SESSION DELETE ANY' in c
    assert 'CLOSED SESSION COMMAND RESTRICT' in c
    assert 'CLOSED SESSION MENTION RESTRICT' in c
    assert 'participation_warning' not in c
    assert 'punish_word' not in c
    assert 'animation' in bot and 'sticker' in bot and 'video_note' in bot and 'audio' in bot
    v=part('validate_referral_after_10min')
    assert 'increment_referral_counter_v16' in v
    assert 'REFERRAL VALIDATED 10MIN' in v
    assert 'grace_presidentielle' not in bot
    assert 'grace_ministerielle' not in bot

if __name__=='__main__':
    test_v16()
    print('V16 SELFTEST OK')
