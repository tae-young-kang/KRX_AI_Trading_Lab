"""Short-lived batch-size hints only: no prices, symbols or credentials."""
from threading import Lock
from time import monotonic


class QuoteBatchProfile:
    def __init__(self,*,clock=monotonic):
        self._clock=clock
        self._lock=Lock()
        self._limits={}

    def limit(self,scope):
        with self._lock:
            value=self._limits.get(scope)
            if value and 0<=self._clock()-value[1]<600:
                return value[0]
            return 100

    def completed(self,scope,limit):
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError('시세 묶음 크기를 확인해 주세요.')
        with self._lock:
            self._limits[scope]=(limit,self._clock())
