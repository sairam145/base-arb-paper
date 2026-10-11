// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/*
 * PaperArb — NEVER deployed. paper.py injects this code at a fake address inside
 * a free eth_call (state override), so the full trade runs against real Base state:
 * real Balancer flash loan, real Uniswap / Aerodrome swaps, real fees and slippage.
 * Returns the profit in WETH wei, or reverts with Short(have, owed) if it would lose.
 */

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
    function approve(address, uint256) external returns (bool);
}
interface IBalancerVault {
    function flashLoan(address, address[] memory, uint256[] memory, bytes memory) external;
}
interface IUniV3Router02 {
    struct ExactInputSingleParams {
        address tokenIn; address tokenOut; uint24 fee; address recipient;
        uint256 amountIn; uint256 amountOutMinimum; uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata) external payable returns (uint256);
}
interface IAeroRouter {
    struct Route { address from; address to; bool stable; address factory; }
    function swapExactTokensForTokens(uint256, uint256, Route[] calldata, address, uint256) external returns (uint256[] memory);
}

contract PaperArb {
    address constant VAULT = 0xBA12222222228d8Ba445958a75a0704d566BF2C8;
    address constant UNI = 0x2626664c2603336E57B271c5C0b26F421741e481;
    address constant AERO = 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43;
    address constant AERO_FACTORY = 0x420DD381b31aEf6683db6B902084cB0FFECe40Da;
    address constant WETH = 0x4200000000000000000000000000000000000006;
    address constant USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;

    error Short(uint256 have, uint256 owed);

    function simulate(uint256 amount, uint8 sellVenue, uint8 buyVenue) external returns (uint256 profit) {
        IERC20(WETH).approve(UNI, type(uint256).max);
        IERC20(USDC).approve(UNI, type(uint256).max);
        IERC20(WETH).approve(AERO, type(uint256).max);
        IERC20(USDC).approve(AERO, type(uint256).max);
        address[] memory t = new address[](1);
        t[0] = WETH;
        uint256[] memory a = new uint256[](1);
        a[0] = amount;
        IBalancerVault(VAULT).flashLoan(address(this), t, a, abi.encode(sellVenue, buyVenue));
        return IERC20(WETH).balanceOf(address(this));
    }

    function receiveFlashLoan(address[] memory, uint256[] memory amounts, uint256[] memory fees, bytes memory data) external {
        require(msg.sender == VAULT, "vault");
        (uint8 s, uint8 b) = abi.decode(data, (uint8, uint8));
        uint256 usdcOut = _swap(s, WETH, USDC, amounts[0]);
        _swap(b, USDC, WETH, usdcOut);
        uint256 owed = amounts[0] + fees[0];
        uint256 have = IERC20(WETH).balanceOf(address(this));
        if (have < owed) revert Short(have, owed);
        IERC20(WETH).transfer(VAULT, owed);
    }

    function _swap(uint8 v, address i, address o, uint256 amt) internal returns (uint256) {
        if (v < 2) {
            return IUniV3Router02(UNI).exactInputSingle(IUniV3Router02.ExactInputSingleParams(
                i, o, v == 0 ? 500 : 3000, address(this), amt, 0, 0));
        }
        IAeroRouter.Route[] memory r = new IAeroRouter.Route[](1);
        r[0] = IAeroRouter.Route(i, o, false, AERO_FACTORY);
        uint256[] memory out = IAeroRouter(AERO).swapExactTokensForTokens(amt, 0, r, address(this), block.timestamp);
        return out[out.length - 1];
    }
}
