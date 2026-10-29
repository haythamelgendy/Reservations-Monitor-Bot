import os
os.environ.setdefault('TELEGRAM_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', 'test-chat')
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import patch, Mock
import monitor as m


def movie(ids, title='A & B <Preview>'):
    return {'title': title, 'rating': '12+', 'language': 'English',
            'duration': '120 min', 'info_link': 'https://egy.voxcinemas.com/movies/test?a=1&b=2',
            'halls': [{'name': 'IMAX', 'showtimes': [
                {'booking_id': str(i), 'time': '8:00pm',
                 'link': f'https://egy.voxcinemas.com/booking/0047-{i}'} for i in ids]}]}


class AlertBehavior(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.file_patch = patch.object(m, 'SENT_FILE', self.temp.name + '/sent.txt')
        self.file_patch.start()
        m.MONITOR_ERRORS.clear()
        self.day = date(2100, 1, 2)

    def tearDown(self):
        self.file_patch.stop()
        self.temp.cleanup()
        m.MONITOR_ERRORS.clear()

    def test_new_movie_and_added_showtime_without_repeating_old(self):
        sent = set()
        with patch.object(m, 'send_telegram_message', return_value=True) as send:
            self.assertEqual(m.process_movies_for_date([movie([1])], self.day, sent), 1)
            self.assertEqual(m.process_movies_for_date([movie([1, 2])], self.day, sent), 1)
            self.assertEqual(m.process_movies_for_date([movie([3], 'New movie')], self.day, sent), 1)
            self.assertEqual(send.call_count, 3)
        self.assertEqual(len(m.load_sent_items()), 3)

    def test_delivery_failure_does_not_lose_alert(self):
        sent = set()
        with patch.object(m, 'send_telegram_message', return_value=False):
            self.assertEqual(m.process_movies_for_date([movie([1])], self.day, sent), 0)
        self.assertEqual(sent, set())
        self.assertEqual(m.load_sent_items(), set())
        with patch.object(m, 'send_telegram_message', return_value=True):
            self.assertEqual(m.process_movies_for_date([movie([1])], self.day, sent), 1)

    def test_restart_uses_persisted_history(self):
        with patch.object(m, 'send_telegram_message', return_value=True):
            m.process_movies_for_date([movie([1])], self.day, set())
        with patch.object(m, 'send_telegram_message') as send:
            self.assertEqual(m.process_movies_for_date([movie([1])], self.day, m.load_sent_items()), 0)
            send.assert_not_called()

    def test_partial_delivery_only_records_successful_batch(self):
        sent = set()
        with patch.object(m, 'send_telegram_message', side_effect=[False, True]):
            self.assertEqual(m.process_movies_for_date([movie(range(13))], self.day, sent), 1)
        self.assertEqual(sent, {'vox:2100-01-02:12'})

    def test_duplicate_sessions_in_page_only_send_once(self):
        sent = set()
        with patch.object(m, 'send_telegram_message', return_value=True):
            self.assertEqual(m.process_movies_for_date([movie([1, 1])], self.day, sent), 1)

    def test_html_is_escaped(self):
        item = movie([1])
        message = m.format_vox_message(item, 'Today', {'IMAX': item['halls'][0]['showtimes']})
        self.assertIn('A &amp; B &lt;Preview&gt;', message)
        self.assertIn('a=1&amp;b=2', message)

    def test_expired_3d_showtime_is_not_notified(self):
        now = datetime(2100, 1, 2, 21, 0, tzinfo=m.EGYPT_TZ)
        item = movie([1])
        item['halls'][0]['showtimes'][0]['time'] = '8:00pm 3D'
        with patch.object(m, 'now_egypt', return_value=now), patch.object(m, 'send_telegram_message') as send:
            self.assertEqual(m.process_movies_for_date([item], now.date(), set()), 0)
            send.assert_not_called()

    def test_telegram_must_confirm_ok(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {'ok': False}
        with patch.object(m.requests, 'post', return_value=response):
            self.assertFalse(m.send_telegram_message('test'))
        self.assertTrue(m.MONITOR_ERRORS)
        m.MONITOR_ERRORS.clear()
        response.json.return_value = {'ok': True}
        with patch.object(m.requests, 'post', return_value=response):
            self.assertTrue(m.send_telegram_message('test'))


if __name__ == '__main__':
    unittest.main()
