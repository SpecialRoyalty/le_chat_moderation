from pathlib import Path
bot=Path('bot.py').read_text(encoding='utf-8')

def test_v5():
    assert 'FINAL_CLEAN_V5_SESSION_CLOSED_REDIF_FIX' in bot
    assert 'closed_session_block' in bot
    assert 'JOIN_LEFT SERVICE DELETE' in bot
    assert 'send_super_trusted_report' in bot
    assert 'REDIFFUSION COPY OK' in bot
    assert '🟢 Participation ON' in bot
    assert 'Messages supprimés :' not in bot
    assert 'grace_presidentielle' not in bot
    assert 'grace_ministerielle' not in bot

if __name__=='__main__':
    test_v5()
    print('V5 SELFTEST OK')
