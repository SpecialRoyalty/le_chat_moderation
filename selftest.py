from pathlib import Path
bot=Path('bot.py').read_text(encoding='utf-8')

def test_v13():
    assert 'FINAL_CLEAN_V13_NAME_FIXES_AUTO_LOGS' in bot
    assert 'async def ban_for_forbidden_username' in bot
    assert 'def back_kb' in bot
    assert 'AUTO SCHEDULE DEBUG' in bot
    assert 'AUTO OPENING REMINDER CHECK no_send' in bot
    assert 'error_handler_v13' in bot
    assert 'add_error_handler(error_handler_v13' in bot
    assert 'grace_presidentielle' not in bot
    assert 'grace_ministerielle' not in bot

if __name__=='__main__':
    test_v13()
    print('V13 SELFTEST OK')
