from pathlib import Path
bot=Path('bot.py').read_text(encoding='utf-8')

def part(name, async_def=True):
    marker=('async def ' if async_def else 'def ')+name
    s=bot.index(marker)
    e=bot.find('\nasync def ', s+1)
    e2=bot.find('\ndef ', s+1)
    ends=[x for x in [e,e2] if x!=-1]
    end=min(ends) if ends else len(bot)
    return bot[s:end]

def test_v14():
    assert 'FINAL_CLEAN_V14_HASH_MEDIA_WAIT_FIX' in bot
    assert 'add_banned_hashes_from_message_v14' in bot
    assert 'HASH MEDIA WAIT SET' in bot
    assert 'HASH MEDIA WAIT STORED' in bot
    hp=part('private_admin')
    assert "state=='hash_media_wait'" in hp
    assert 'Hash identique' in hp
    assert 'add_banned_hashes_from_message_v14' in hp
    app=part('build_app', async_def=False)
    assert 'filters.PHOTO' in app and 'filters.VIDEO' in app
    assert 'grace_presidentielle' not in bot
    assert 'grace_ministerielle' not in bot

if __name__=='__main__':
    test_v14()
    print('V14 SELFTEST OK')
