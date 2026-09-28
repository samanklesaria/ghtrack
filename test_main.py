from datetime import datetime, timedelta, timezone

from main import commit_activity_date, mine_and_recent


def stamp(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def commit(author=None, committer=None, authored=0, committed=0):
    return {
        'author': {'login': author} if author else None,
        'committer': {'login': committer} if committer else None,
        'commit': {
            'author': {'date': stamp(authored)},
            'committer': {'date': stamp(committed)},
        },
    }


def test_force_pushed_commit_counts_under_its_committer_date():
    # A rebase replays a 60-day-old authored date but stamps the committer date today
    assert commit_activity_date(commit('me', 'me', authored=60, committed=1), 'me', 9)
    assert not commit_activity_date(commit('me', 'me', authored=60, committed=60), 'me', 9)


def test_rebasing_someone_elses_commit_counts_as_mine():
    assert commit_activity_date(commit('them', 'me', authored=60, committed=1), 'me', 9)
    assert not commit_activity_date(commit('them', 'them', authored=1, committed=1), 'me', 9)


def test_unlinked_author_does_not_crash():
    assert commit_activity_date(commit(None, None), 'me', 9) is None


def test_approval_with_no_body_is_kept():
    review = {'user': {'login': 'me'}, 'submitted_at': stamp(1), 'body': '', 'state': 'APPROVED'}
    kept = mine_and_recent([review], 'me', 9)
    assert len(kept) == 1 and kept[0]['body'] == 'approved'


def test_pending_and_stale_and_other_people_are_dropped():
    pending = {'user': {'login': 'me'}, 'submitted_at': stamp(1), 'body': 'wip', 'state': 'PENDING'}
    stale = {'user': {'login': 'me'}, 'created_at': stamp(60), 'body': 'old'}
    theirs = {'user': {'login': 'them'}, 'created_at': stamp(1), 'body': 'hi'}
    assert mine_and_recent([pending, stale, theirs], 'me', 9) == []
