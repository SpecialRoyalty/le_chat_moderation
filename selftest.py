from pathlib import Path
bot = Path('bot.py').read_text(encoding='utf-8')

def test_v62():
    assert 'FINAL_COMPLETE_V62_FAST_TRUSTED_PRIORITY' in bot
    assert 'def run_bg' in bot
    assert 'BACKGROUND TASK STARTED' in bot
    assert 'FAST PRIORITY HASH BAN' in bot
    assert 'trusted_supprime' in bot
    assert 'trusted_ban' in bot
    assert 'trusted_pedo' in bot

if __name__ == '__main__':
    test_v62()
    print('V62 SELFTEST OK')
