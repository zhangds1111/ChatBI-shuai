-- 在 csv 文件夹的上一级目录执行,并开启 local_infile:
--   mysql --local-infile=1 -u <用户> -p chatbi_demo < load_data.sql
USE chatbi_demo;

LOAD DATA LOCAL INFILE 'csv/dim_stores.csv' INTO TABLE `dim_stores` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES
(`store_id`, `store_name`, `marketplace`, `currency`, `timezone`, `owner`);

LOAD DATA LOCAL INFILE 'csv/dim_products.csv' INTO TABLE `dim_products` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES
(`store_id`, `msku`, `sku`, `asin`, `parent_asin`, `product_name`, `category`, `owner`, `unit_cost`, `head_freight_cost`, `launch_date`, `status`);

LOAD DATA LOCAL INFILE 'csv/exchange_rates.csv' INTO TABLE `exchange_rates` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES
(`rate_date`, `currency`, `rate_to_cny`);

LOAD DATA LOCAL INFILE 'csv/amz_order_items.csv' INTO TABLE `amz_order_items` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES
(`order_id`, `order_item_id`, `store_id`, `msku`, `asin`, `purchase_date`, `order_status`, `quantity`, `item_amount`, `tax_amount`, `shipping_amount`, `promotion_amount`, `currency`);

LOAD DATA LOCAL INFILE 'csv/amz_ads_daily.csv' INTO TABLE `amz_ads_daily` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES
(`report_date`, `store_id`, `ad_type`, `campaign_id`, `campaign_name`, `asin`, `msku`, `impressions`, `clicks`, `spend`, `sales_7d`, `orders_7d`, `units_7d`, `currency`);

LOAD DATA LOCAL INFILE 'csv/amz_traffic_daily.csv' INTO TABLE `amz_traffic_daily` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES
(`report_date`, `store_id`, `asin`, `sessions`, `page_views`, `units_ordered`, `buy_box_pct`, `ordered_sales`);

LOAD DATA LOCAL INFILE 'csv/amz_fba_inventory.csv' INTO TABLE `amz_fba_inventory` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES
(`snapshot_date`, `store_id`, `msku`, `asin`, `available_qty`, `inbound_qty`, `reserved_qty`, `age_0_90_qty`, `age_91_180_qty`, `age_181_270_qty`, `age_271_365_qty`, `age_365_plus_qty`);

LOAD DATA LOCAL INFILE 'csv/amz_settlement.csv' INTO TABLE `amz_settlement` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES
(`settlement_id`, `posted_date`, `store_id`, `order_id`, `msku`, `fee_type`, `amount`, `currency`);

LOAD DATA LOCAL INFILE 'csv/amz_returns.csv' INTO TABLE `amz_returns` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES
(`return_date`, `store_id`, `order_id`, `msku`, `asin`, `quantity`, `reason`, `disposition`);
