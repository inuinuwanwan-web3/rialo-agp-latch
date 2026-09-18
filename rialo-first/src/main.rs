use rialo_cdk::RpcClient;
use rialo_cdk::rpc::HttpRpcClient;

#[tokio::main]
async fn main() -> rialo_cdk::Result<()> {
    let client = HttpRpcClient::new(
        rialo_cdk::constants::URL_DEVNET.to_string()
    );

    let config_hash = client.get_config_hash_prefix().await?;

    println!("Rialo DevNet connected!");
    println!("Config hash: {:?}", config_hash);

    Ok(())
}
