"""Human-readable aircraft requests; never relabel RC as measured velocity."""

MODE_NAMES = {
    "yaw": "转向跟随（不横移）",
    "lateral": "横移跟随（不转向，待实飞）",
    "hybrid": "转向＋横移（共用横向请求，待实飞）",
    "adaptive": "横移为主，偏到画面边缘时加强转向（待实飞）",
}


def motion_text(channels, steering_mode):
    """Directions use the camera/aircraft viewpoint, not the facing person's."""
    if steering_mode not in MODE_NAMES:
        raise ValueError("unknown steering mode")
    if len(channels) != 4 or any(type(v) is not int or not -100 <= v <= 100
                                 for v in channels):
        raise ValueError("four integer RC channels required")
    right, forward, up, yaw = channels

    def line(value, positive, negative, zero):
        return (f"飞机{positive if value > 0 else negative}：{abs(value)} RC"
                if value else zero + "：0 RC")

    return "\n".join((
        "模拟飞机指令（RC控制量，不是米/秒）",
        MODE_NAMES[steering_mode],
        line(forward, "前进", "后退", "前后请求归零"),
        line(yaw, "右转", "左转", "转向请求归零"),
        line(right, "右移", "左移", "横移请求归零"),
        line(up, "上升", "下降", "升降请求归零"),
        "方向以飞机看出去为准。靠得太近时，后退是为了保持距离。",
    ))


def distance_text(result):
    if result.get("status") == "estimated" and result.get("distance_m") is not None:
        return (f"估计光轴距离：{result['distance_m']:.2f} 米\n"
                "仅适用于同一相机、同一实体标记、近似正对；不是精确测距。")
    reason = result.get("reason", "not_calibrated")
    if reason == "not_calibrated":
        return "距离：未标定（当前只保持画面大小）\n实际速度：未测量，不能由RC直接换算。"
    labels = {
        "target_unavailable": "没有有效目标",
        "stale_frame": "画面过期",
        "future_frame": "画面时间戳异常",
        "geometry_unsuitable": "标记侧转或形状不可靠",
        "size_geometry_mismatch": "标记尺寸与角点不一致",
        "reference_size_changed": "参考大小已改变，请重新标定",
        "marker_key_mismatch": "已更换实体标记，请重新标定",
        "camera_key_mismatch": "相机来源已改变，请重新标定",
        "image_width_mismatch": "相机分辨率已改变，请重新标定",
        "image_height_mismatch": "相机分辨率已改变，请重新标定",
        "target_id_mismatch": "标记编号不匹配",
        "invalid_calibration": "标定记录无效",
    }
    return "距离估计不可用：" + labels.get(reason, reason) + "\n实际速度：未测量。"


def depth_status_text(guard):
    reasons = {
        "clear": "目标形状有效",
        "target_unavailable": "未获得有效目标，前后归零",
        "geometry_confirmation_pending": "等待确认目标形状，前后归零",
        "geometry_missing_or_invalid": "目标角点不可靠，前后归零",
        "shape_ambiguous": "标记侧转或形状不可靠，前后归零",
        "waiting_for_front_facing_target": "等待标记转正，前后归零",
        "waiting_for_stable_shape": "正在确认形状稳定，前后归零",
    }
    return reasons.get(guard.get("reason"), "形状检查：" + str(guard.get("reason")))


def braking_text(status):
    labels = {
        'approach_braking': '正在提前减速／制动',
        'depth_trend_response': '正在按距离变化调整请求',
        'small_reversal_zero': '小幅反向请求暂时归零，避免来回跳动',
        'insufficient_consistent_trend': '沿用 PID，等待可靠的变化趋势',
        'geometry_blocked_or_legacy_distance': '未启用趋势修正（检查距离模式或目标形状）',
    }
    reason=status.get('reason','')
    return '前后制动：'+labels.get(reason,'未积累运动趋势')
