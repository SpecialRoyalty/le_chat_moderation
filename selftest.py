from pathlib import Path
bot=Path('bot.py').read_text(encoding='utf-8')

def test_v6():
    assert 'FINAL_CLEAN_V6_GLOBAL_STATUS_AUTO_MIDSCAN' in bot
    assert 'session_status_message_id' in bot
    assert 'SESSION GLOBAL STATUS EDIT' in bot
    assert 'maybe_mid_session_nonparticipant_prompt' in bot
    assert 'NON_PARTICIPANT MIDSCAN' in bot
    assert 'Rediffusion impossible' in bot
    assert 'grace_presidentielle' not in bot
    assert 'grace_ministerielle' not in bot

if __name__=='__main__':
    test_v6()
    print('V6 SELFTEST OK')
