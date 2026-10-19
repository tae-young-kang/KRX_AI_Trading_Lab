"""GUI methods with fake labels/buttons only; never constructs QApplication."""
from types import SimpleNamespace
from test_recommendation_request import gui_method


class Widget:
    def __init__(self):self.value='';self.enabled=None
    def setText(self,text):self.value=text
    def text(self):return self.value
    def setEnabled(self,value):self.enabled=value


def widgets():
    fake=SimpleNamespace(**{name:Widget() for name in (
        'telegram_order_status','home_telegram_status','home_telegram_start','home_telegram_stop',
        'telegram_order_start','telegram_order_stop','telegram_emergency','telegram_resume')})
    fake.set_telegram_status=lambda text:gui_method('set_telegram_status')(fake,text)
    return fake


def test_home_mirrors_receiver_status_and_keeps_failure_visible():
    fake=widgets()
    fake.telegram_order_worker=SimpleNamespace(service=object(),isRunning=lambda:True,
        isInterruptionRequested=lambda:False,orders_enabled=False)
    gui_method('telegram_orders_status')(fake,'수신 중 · 조건·조회 관리')
    assert fake.home_telegram_status.text()==fake.telegram_order_status.text()
    assert not fake.telegram_emergency.enabled and not fake.telegram_resume.enabled
    error='수신 중지 · 연결 정보를 확인하세요.'
    gui_method('telegram_orders_status')(fake,error)
    gui_method('telegram_orders_finished')(fake)
    assert fake.home_telegram_status.text()==error
    assert fake.home_telegram_start.enabled and not fake.home_telegram_stop.enabled
    assert fake.telegram_order_start.enabled and not fake.telegram_order_stop.enabled


def test_home_stop_uses_same_receiver_stop_and_waits_for_finished():
    fake=widgets();calls=[]
    fake.telegram_order_worker=SimpleNamespace(stop=lambda:calls.append('stop'))
    fake.home_telegram_start.setEnabled(False)
    gui_method('stop_telegram_orders')(fake)
    assert calls==['stop']
    assert not fake.home_telegram_start.enabled and not fake.home_telegram_stop.enabled
    assert '수신 중지 요청' in fake.home_telegram_status.text()
    gui_method('telegram_orders_finished')(fake)
    assert fake.home_telegram_start.enabled
    assert fake.home_telegram_status.text()==fake.telegram_order_status.text()
