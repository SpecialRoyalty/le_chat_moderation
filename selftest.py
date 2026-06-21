from pathlib import Path
bot=Path('bot.py').read_text(encoding='utf-8')

def test_v9():
    assert 'FINAL_CLEAN_V9_RULES_GROUP_BROADCAST' in bot
    assert 'group_broadcast_set' in bot
    assert 'rules_menu' in bot
    assert 'rule1_text' in bot and 'rule2_text' in bot
    assert 'maybe_publish_auto_rules' in bot
    assert 'cleanup_rules_messages' in bot
    assert 'GROUP BROADCAST SENT' in bot
    assert 'RULES AUTO SCHEDULED' in bot
    assert 'grace_presidentielle' not in bot
    assert 'grace_ministerielle' not in bot

if __name__=='__main__':
    test_v9()
    print('V9 SELFTEST OK')
