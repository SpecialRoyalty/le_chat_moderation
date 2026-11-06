"""Pure functions for VIP invitation unlocks and raffle validation."""
from __future__ import annotations

from decimal import Decimal


def goal_progress(credited: int, goal: int) -> tuple[int, int, bool]:
    if goal <= 0 or goal > 100000:
        raise ValueError('Objectif d’invitations invalide')
    credited=max(0,int(credited))
    return min(credited,goal), goal, credited>=goal


def can_buy(price, invite_goal=0):
    return price is not None and Decimal(str(price)) > 0


def is_claimable(offer:dict, credited: int, member_is_present: bool, already_claimed:bool=False)->bool:
    goal=int(offer.get('invite_goal',0))
    return (bool(offer.get('enabled')) and member_is_present and
            not already_claimed and goal>0 and credited>=goal)


def raffle_eligible(rows, minimum: int) -> list[dict]:
    if not 1 <= int(minimum) <= 10000:
        raise ValueError('Seuil de participation invalide')
    return [row for row in rows if int(row['n'])>=int(minimum)]
