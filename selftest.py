from pathlib import Path
bot=Path('bot.py').read_text(encoding='utf-8')

def part(name):
    s=bot.index(f'async def {name}')
    e=bot.find('\nasync def ', s+1)
    return bot[s:e if e!=-1 else len(bot)]

def test_v8():
    assert 'FINAL_CLEAN_V8_HASH_PRIORITY_SAFE' in bot
    f=part('process_media_priority_v8')
    assert f.index('any_banned') < f.index('any_existing')
    assert 'V8 HASH PRIORITY: BANNED_HASH FIRST' in bot
    assert 'V8 HASH PRIORITY: ANTI_REPOST AFTER_BANNED_CLEAR' in bot
    h=part('handle_group_message')
    assert h.index('process_media_priority_v8') < h.index('record_media')
    c=part('closed_session_block')
    assert c.index('process_media_priority_v8') < c.index('record_media')
    assert 'grace_presidentielle' not in bot
    assert 'grace_ministerielle' not in bot

if __name__=='__main__':
    test_v8()
    print('V8 SELFTEST OK')
