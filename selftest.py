from pathlib import Path
bot=Path('bot.py').read_text(encoding='utf-8')

def test_v4_session_fix():
    assert 'FINAL_CLEAN_V4_SESSION_FIX' in bot
    assert 'send_or_edit_session_status' in bot
    assert 'purge_session_messages' in bot
    assert 'SESSION DELETE START' in bot
    assert "callback_data='hard_menu'" in bot
    assert "callback_data='users_menu'" in bot
    assert "callback_data='share_menu'" in bot
    assert 'schedule_json' in bot
    assert 'grace_presidentielle' not in bot
    assert 'grace_ministerielle' not in bot

if __name__=='__main__':
    test_v4_session_fix()
    print('V4 SELFTEST OK')
