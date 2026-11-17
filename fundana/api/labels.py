"""向后兼容的转发层：标签字典已移到 fundana/labels.py（publish 也要用）。

保留这个模块名是为了让 fundana/api/* 的既有 import 不用改。
"""
from ..labels import (  # noqa: F401
    BOND_CLASS_LABELS, BOARD_LABELS, BUYABLE_LABELS, CONFIDENCE_LABELS, FETCH_STATUS_LABELS,
    FIELD_LABELS, KIND_LABELS, LAYER_LABELS, REASON_LABELS, RETURN_TYPE_LABELS,
    PROFILE_LABELS, RUN_MODE_LABELS, RUN_STATUS_LABELS, SCALE_SOURCE_LABELS, SOURCE_LABELS,
    STYLE_LABELS,
    USER_FIT_LABELS, label_board, label_bond_class, label_buyable, label_confidence,
    label_fetch_status, label_layer, label_profile, label_reason, label_return_type,
    label_run_mode, label_run_status, label_scale_source, label_source, label_style,
    label_lockup,
    label_user_fit,
    labels_payload,
)
