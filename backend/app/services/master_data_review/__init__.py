"""主数据审核共享服务包。

统一「主数据审核」复用模式（避免逻辑度量/维度/术语三套重复代码）：
- ``schemas.ReviewSubmitRequest / ReviewApproveRequest / ReviewRejectRequest``：
  三实体共用审核请求结构（评审指派/通过意见/驳回原因）；
- ``service.MasterDataReviewMixin``：submit/approve/reject 状态机 + 评审权校验 +
  自审禁止 + 通知的共享实现，宿主服务配置少量类属性即可复用。
"""

from app.services.master_data_review.service import MasterDataReviewMixin

__all__ = ["MasterDataReviewMixin"]
