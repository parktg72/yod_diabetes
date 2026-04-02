"""
gpu_accelerator.py - GPU 가속 유틸리티
cupy + PyTorch 기반 GPU 가속. GPU 미설치 시 CPU 자동 폴백.
"""

import logging
import numpy as np
from config import GPU_SETTINGS

logger = logging.getLogger(__name__)

# -------------------------------------------------------
# GPU 라이브러리 감지
# -------------------------------------------------------
_CUPY_AVAILABLE = False
_TORCH_AVAILABLE = False
_TORCH_CUDA = False

try:
    import cupy as cp
    if cp.cuda.is_available():
        _CUPY_AVAILABLE = True
        logger.info(f"cupy GPU 사용 가능: {cp.cuda.runtime.getDeviceProperties(0)['name'].decode()}")
except (ImportError, Exception):
    pass

try:
    import torch
    if torch.cuda.is_available():
        _TORCH_AVAILABLE = True
        _TORCH_CUDA = True
        logger.info(f"PyTorch CUDA 사용 가능: {torch.cuda.get_device_name(0)}")
    else:
        _TORCH_AVAILABLE = True
        _TORCH_CUDA = False
except ImportError:
    pass


def is_gpu_enabled():
    """GPU 사용이 설정되어 있고 실제 사용 가능한지 확인"""
    return GPU_SETTINGS.get('USE_GPU', False) and (_CUPY_AVAILABLE or _TORCH_CUDA)


def get_gpu_status():
    """GPU 상태 정보 반환"""
    return {
        'gpu_enabled': is_gpu_enabled(),
        'cupy_available': _CUPY_AVAILABLE,
        'torch_available': _TORCH_AVAILABLE,
        'torch_cuda': _TORCH_CUDA,
        'settings': GPU_SETTINGS,
    }


# -------------------------------------------------------
# numpy ↔ cupy 호환 함수
# -------------------------------------------------------
def to_gpu(arr):
    """numpy 배열을 GPU로 전송 (가능하면)"""
    if is_gpu_enabled() and _CUPY_AVAILABLE:
        import cupy as cp
        return cp.asarray(arr)
    return np.asarray(arr)


def to_cpu(arr):
    """GPU 배열을 CPU numpy로 변환"""
    if _CUPY_AVAILABLE and hasattr(arr, 'get'):
        return arr.get()
    return np.asarray(arr)


def get_array_module(arr=None):
    """배열에 맞는 모듈(numpy/cupy) 반환"""
    if arr is not None and _CUPY_AVAILABLE:
        import cupy as cp
        return cp.get_array_module(arr)
    if is_gpu_enabled() and _CUPY_AVAILABLE:
        import cupy as cp
        return cp
    return np


# -------------------------------------------------------
# GPU 가속 PSM: LogisticRegression
# -------------------------------------------------------
class GPULogisticRegression:
    """PyTorch 기반 GPU 로지스틱 회귀 (sklearn API 호환)"""

    def __init__(self, max_iter=1000, lr=0.01, random_state=42):
        self.max_iter = max_iter
        self.lr = lr
        self.random_state = random_state
        self.weights = None
        self.bias = None
        self._device = None

    def fit(self, X, y):
        import torch
        torch.manual_seed(self.random_state)
        self._device = torch.device('cuda' if _TORCH_CUDA else 'cpu')

        X_t = torch.tensor(X.values if hasattr(X, 'values') else X,
                           dtype=torch.float32, device=self._device)
        y_t = torch.tensor(y.values if hasattr(y, 'values') else y,
                           dtype=torch.float32, device=self._device)

        n_features = X_t.shape[1]
        self.weights = torch.zeros(n_features, device=self._device, requires_grad=True)
        self.bias = torch.zeros(1, device=self._device, requires_grad=True)

        optimizer = torch.optim.LBFGS([self.weights, self.bias], max_iter=20, lr=self.lr)

        def closure():
            optimizer.zero_grad()
            logits = X_t @ self.weights + self.bias
            loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, y_t)
            # L2 regularization (like sklearn C=1.0)
            loss = loss + 0.5 * torch.sum(self.weights ** 2) / len(y_t)
            loss.backward()
            return loss

        for _ in range(self.max_iter // 20):
            optimizer.step(closure)

        return self

    def predict_proba(self, X):
        import torch
        X_t = torch.tensor(X.values if hasattr(X, 'values') else X,
                           dtype=torch.float32, device=self._device)
        with torch.no_grad():
            logits = X_t @ self.weights + self.bias
            probs = torch.sigmoid(logits).cpu().numpy()
        return np.column_stack([1 - probs, probs])


# -------------------------------------------------------
# GPU 가속 PSM: NearestNeighbors
# -------------------------------------------------------
class GPUNearestNeighbors:
    """PyTorch 기반 GPU kNN (sklearn API 호환)"""

    def __init__(self, n_neighbors=5, metric='euclidean'):
        self.n_neighbors = n_neighbors
        self._data = None
        self._device = None

    def fit(self, X):
        import torch
        self._device = torch.device('cuda' if _TORCH_CUDA else 'cpu')
        self._data = torch.tensor(
            X.values if hasattr(X, 'values') else X,
            dtype=torch.float32, device=self._device
        )
        return self

    def kneighbors(self, X, return_distance=True):
        import torch
        X_t = torch.tensor(
            X.values if hasattr(X, 'values') else X,
            dtype=torch.float32, device=self._device
        )

        # Batch processing to avoid GPU OOM on large datasets
        batch_size = min(5000, len(X_t))
        all_dists = []
        all_idxs = []

        for start in range(0, len(X_t), batch_size):
            end = min(start + batch_size, len(X_t))
            batch = X_t[start:end]

            # Euclidean distance: ||a - b||^2 = ||a||^2 - 2*a*b + ||b||^2
            a2 = (batch ** 2).sum(dim=1, keepdim=True)
            b2 = (self._data ** 2).sum(dim=1, keepdim=True).T
            dist_sq = a2 - 2 * batch @ self._data.T + b2
            dist_sq = torch.clamp(dist_sq, min=0)  # numerical stability
            dists = torch.sqrt(dist_sq)

            k = min(self.n_neighbors, dists.shape[1])
            top_dists, top_idxs = torch.topk(dists, k, dim=1, largest=False)

            all_dists.append(top_dists.cpu().numpy())
            all_idxs.append(top_idxs.cpu().numpy())

        dists_out = np.vstack(all_dists)
        idxs_out = np.vstack(all_idxs)

        if return_distance:
            return dists_out, idxs_out
        return idxs_out


# -------------------------------------------------------
# GPU 가속 CIF 계산
# -------------------------------------------------------
def compute_cif_gpu(times, event_type):
    """GPU 가속 Aalen-Johansen CIF 추정

    GPU가 사용 가능하면 cupy로 배열 정렬 및 집계를 가속.
    CPU 폴백 시 numpy 사용.
    """
    xp = get_array_module()
    times = xp.asarray(times, dtype=xp.float64)
    event_type = xp.asarray(event_type, dtype=xp.int32)

    order = xp.argsort(times)
    t_sorted = times[order]
    e_sorted = event_type[order]
    n = len(times)

    # GPU에서 unique event times 추출
    event_mask = e_sorted > 0
    unique_event_times = xp.unique(t_sorted[event_mask])

    cif1_list = []
    cif2_list = []
    surv = 1.0
    cum_inc1 = 0.0
    cum_inc2 = 0.0
    ptr = 0

    # unique_event_times를 CPU로 가져와서 루프 (루프 자체는 짧음)
    uet_cpu = to_cpu(unique_event_times) if hasattr(unique_event_times, 'get') else unique_event_times

    for ut in uet_cpu:
        # 벡터화된 비교는 GPU에서 수행
        ut_val = float(ut)
        while ptr < n and float(to_cpu(t_sorted[ptr])) < ut_val:
            ptr += 1
        at_risk = n - ptr

        d1 = int(xp.sum((t_sorted == ut_val) & (e_sorted == 1)))
        d2 = int(xp.sum((t_sorted == ut_val) & (e_sorted == 2)))

        if at_risk > 0:
            cum_inc1 += surv * d1 / at_risk
            cum_inc2 += surv * d2 / at_risk
            surv *= (1 - (d1 + d2) / at_risk)

        cif1_list.append(cum_inc1)
        cif2_list.append(cum_inc2)

    return (
        np.asarray(to_cpu(unique_event_times)),
        np.array(cif1_list),
        np.array(cif2_list)
    )


# -------------------------------------------------------
# 팩토리 함수
# -------------------------------------------------------
def get_logistic_regression(**kwargs):
    """GPU/CPU에 맞는 LogisticRegression 반환"""
    if is_gpu_enabled() and _TORCH_CUDA:
        logger.info("GPU LogisticRegression 사용 (PyTorch CUDA)")
        return GPULogisticRegression(
            max_iter=kwargs.get('max_iter', 1000),
            random_state=kwargs.get('random_state', 42)
        )
    from sklearn.linear_model import LogisticRegression
    return LogisticRegression(**kwargs)


def get_nearest_neighbors(**kwargs):
    """GPU/CPU에 맞는 NearestNeighbors 반환"""
    if is_gpu_enabled() and _TORCH_CUDA:
        logger.info("GPU NearestNeighbors 사용 (PyTorch CUDA)")
        return GPUNearestNeighbors(
            n_neighbors=kwargs.get('n_neighbors', 5),
            metric=kwargs.get('metric', 'euclidean')
        )
    from sklearn.neighbors import NearestNeighbors
    return NearestNeighbors(**kwargs)
