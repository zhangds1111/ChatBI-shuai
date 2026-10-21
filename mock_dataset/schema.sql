-- Amazon chatBI 模拟数据库(MySQL 8.x)。数据为随机生成,不代表真实业务。
CREATE DATABASE IF NOT EXISTS chatbi_demo DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE chatbi_demo;

DROP TABLE IF EXISTS `dim_stores`;
CREATE TABLE `dim_stores` (
  `store_id` VARCHAR(32) NOT NULL COMMENT '店铺站点ID',
  `store_name` VARCHAR(100) COMMENT '店铺名称',
  `marketplace` VARCHAR(8) COMMENT '站点',
  `currency` VARCHAR(8) COMMENT '站点币种',
  `timezone` VARCHAR(40) COMMENT '站点时区',
  `owner` VARCHAR(50) COMMENT '运营负责人',
  PRIMARY KEY (`store_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='店铺站点维表';

DROP TABLE IF EXISTS `dim_products`;
CREATE TABLE `dim_products` (
  `store_id` VARCHAR(32) NOT NULL COMMENT '店铺站点ID',
  `msku` VARCHAR(64) NOT NULL COMMENT 'MSKU',
  `sku` VARCHAR(64) COMMENT '内部SKU',
  `asin` VARCHAR(16) COMMENT 'ASIN',
  `parent_asin` VARCHAR(16) COMMENT '父ASIN',
  `product_name` VARCHAR(200) COMMENT '商品名称',
  `category` VARCHAR(64) COMMENT '品类',
  `owner` VARCHAR(50) COMMENT '产品负责人',
  `unit_cost` DECIMAL(12,4) COMMENT '单件采购成本(CNY)',
  `head_freight_cost` DECIMAL(12,4) COMMENT '单件头程运费(CNY)',
  `launch_date` DATE COMMENT '上架日期',
  `status` VARCHAR(16) COMMENT '商品状态',
  PRIMARY KEY (`store_id`, `msku`),
  KEY `idx_asin` (`asin`),
  KEY `idx_sku` (`sku`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品维表';

DROP TABLE IF EXISTS `exchange_rates`;
CREATE TABLE `exchange_rates` (
  `rate_date` DATE NOT NULL COMMENT '汇率日期',
  `currency` VARCHAR(8) NOT NULL COMMENT '币种',
  `rate_to_cny` DECIMAL(12,6) COMMENT '兑人民币汇率',
  PRIMARY KEY (`rate_date`, `currency`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='汇率表';

DROP TABLE IF EXISTS `amz_order_items`;
CREATE TABLE `amz_order_items` (
  `order_id` VARCHAR(32) NOT NULL COMMENT '订单号',
  `order_item_id` VARCHAR(32) NOT NULL COMMENT '订单商品行ID',
  `store_id` VARCHAR(32) COMMENT '店铺站点ID',
  `msku` VARCHAR(64) COMMENT 'MSKU',
  `asin` VARCHAR(16) COMMENT 'ASIN',
  `purchase_date` DATE COMMENT '下单日期',
  `order_status` VARCHAR(20) COMMENT '订单状态',
  `quantity` INT COMMENT '件数',
  `item_amount` DECIMAL(14,2) COMMENT '商品销售额(原币,不含税)',
  `tax_amount` DECIMAL(14,2) COMMENT '税额(原币)',
  `shipping_amount` DECIMAL(14,2) COMMENT '运费收入(原币)',
  `promotion_amount` DECIMAL(14,2) COMMENT '促销折扣(原币)',
  `currency` VARCHAR(8) COMMENT '币种',
  PRIMARY KEY (`order_id`, `order_item_id`),
  KEY `idx_purchase_date` (`purchase_date`),
  KEY `idx_store_msku` (`store_id`, `msku`),
  KEY `idx_asin` (`asin`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单明细表';

DROP TABLE IF EXISTS `amz_ads_daily`;
CREATE TABLE `amz_ads_daily` (
  `report_date` DATE NOT NULL COMMENT '报告日期',
  `store_id` VARCHAR(32) NOT NULL COMMENT '店铺站点ID',
  `ad_type` VARCHAR(8) COMMENT '广告类型',
  `campaign_id` VARCHAR(32) NOT NULL COMMENT '广告活动ID',
  `campaign_name` VARCHAR(200) COMMENT '广告活动名称',
  `asin` VARCHAR(16) NOT NULL COMMENT '被推广ASIN',
  `msku` VARCHAR(64) COMMENT '被推广MSKU',
  `impressions` BIGINT COMMENT '曝光量',
  `clicks` BIGINT COMMENT '点击量',
  `spend` DECIMAL(14,2) COMMENT '广告花费(原币)',
  `sales_7d` DECIMAL(14,2) COMMENT '7天归因广告销售额(原币)',
  `orders_7d` INT COMMENT '7天归因广告订单数',
  `units_7d` INT COMMENT '7天归因广告销量',
  `currency` VARCHAR(8) COMMENT '币种',
  PRIMARY KEY (`report_date`, `store_id`, `campaign_id`, `asin`),
  KEY `idx_asin` (`asin`),
  KEY `idx_store_date` (`store_id`, `report_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='广告日报表';

DROP TABLE IF EXISTS `amz_traffic_daily`;
CREATE TABLE `amz_traffic_daily` (
  `report_date` DATE NOT NULL COMMENT '报告日期',
  `store_id` VARCHAR(32) NOT NULL COMMENT '店铺站点ID',
  `asin` VARCHAR(16) NOT NULL COMMENT 'ASIN',
  `sessions` INT COMMENT '会话数',
  `page_views` INT COMMENT '页面浏览量',
  `units_ordered` INT COMMENT '下单件数',
  `buy_box_pct` DECIMAL(6,2) COMMENT 'Buy Box占比(%)',
  `ordered_sales` DECIMAL(14,2) COMMENT '下单销售额(原币)',
  PRIMARY KEY (`report_date`, `store_id`, `asin`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='流量日报表';

DROP TABLE IF EXISTS `amz_fba_inventory`;
CREATE TABLE `amz_fba_inventory` (
  `snapshot_date` DATE NOT NULL COMMENT '快照日期',
  `store_id` VARCHAR(32) NOT NULL COMMENT '店铺站点ID',
  `msku` VARCHAR(64) NOT NULL COMMENT 'MSKU',
  `asin` VARCHAR(16) COMMENT 'ASIN',
  `available_qty` INT COMMENT '可售库存',
  `inbound_qty` INT COMMENT '在途库存',
  `reserved_qty` INT COMMENT '预留库存',
  `age_0_90_qty` INT COMMENT '库龄0-90天件数',
  `age_91_180_qty` INT COMMENT '库龄91-180天件数',
  `age_181_270_qty` INT COMMENT '库龄181-270天件数',
  `age_271_365_qty` INT COMMENT '库龄271-365天件数',
  `age_365_plus_qty` INT COMMENT '库龄365天以上件数',
  PRIMARY KEY (`snapshot_date`, `store_id`, `msku`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='FBA库存快照表';

DROP TABLE IF EXISTS `amz_settlement`;
CREATE TABLE `amz_settlement` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增主键(仅模拟库使用)',
  `settlement_id` VARCHAR(32) COMMENT '结算批次ID',
  `posted_date` DATE COMMENT '入账日期',
  `store_id` VARCHAR(32) COMMENT '店铺站点ID',
  `order_id` VARCHAR(32) COMMENT '订单号',
  `msku` VARCHAR(64) COMMENT 'MSKU',
  `fee_type` VARCHAR(24) COMMENT '费用类型',
  `amount` DECIMAL(14,2) COMMENT '金额(原币)',
  `currency` VARCHAR(8) COMMENT '币种',
  PRIMARY KEY (`id`),
  KEY `idx_posted_date` (`posted_date`),
  KEY `idx_order` (`order_id`),
  KEY `idx_fee_type` (`fee_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='结算明细表';

DROP TABLE IF EXISTS `amz_returns`;
CREATE TABLE `amz_returns` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增主键(仅模拟库使用)',
  `return_date` DATE COMMENT '退货日期',
  `store_id` VARCHAR(32) COMMENT '店铺站点ID',
  `order_id` VARCHAR(32) COMMENT '原订单号',
  `msku` VARCHAR(64) COMMENT 'MSKU',
  `asin` VARCHAR(16) COMMENT 'ASIN',
  `quantity` INT COMMENT '退货件数',
  `reason` VARCHAR(100) COMMENT '退货原因',
  `disposition` VARCHAR(30) COMMENT '处置结果',
  PRIMARY KEY (`id`),
  KEY `idx_return_date` (`return_date`),
  KEY `idx_order` (`order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='退货明细表';
