from pathlib import Path
bot=Path('bot.py').read_text(encoding='utf-8')

def test_v11():
    assert 'FINAL_CLEAN_V11_RESTRICT_VISIBILITY' in bot
    assert "CommandHandler('hashmedia'" not in bot
    assert 'async def admin_hashmedia' not in bot
    assert 'hash_media_set' in bot
    assert 'RESTRICTION VISIBILITY BAN' in bot
    assert 'unban_later' in bot
    assert 'is_protected_user' in bot
    assert 'grace_presidentielle' not in bot
    assert 'grace_ministerielle' not in bot

if __name__=='__main__':
    test_v11()
    print('V11 SELFTEST OK')
